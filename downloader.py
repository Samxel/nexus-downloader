import re
import shutil
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import py7zr
from curl_cffi import requests
from rich.console import Group
from rich.live import Live
from rich.text import Text

from auth import get_token
from config import (
    ARCHIVE_EXTENSIONS, CHUNK_SIZE, CONFIG_FILE, NEXUS_SESSION_COOKIE, CONNECT_TIMEOUT_S, DELETE_ARCHIVE_AFTER_EXTRACT, DOWNLOAD_DIR, DOWNLOAD_RETRIES,
    EXTRACT, FILE_CATEGORIES, MAX_PARALLEL_DOWNLOADS, MAX_QUEUED_LINKS, MOD_LIST, READ_TIMEOUT_S, REQUEST_DELAY_S,
    SCAN_DELAY_S, SEVEN_ZIP,
)
from nexus import generate_download_url, mod_file, mod_files, mod_info
from ui import (
    console, finish, fit, hms, log, open_log, progress, record_failure, short, size_str, stats, stats_lock, view,
)

stop_event = threading.Event()
thread_local = threading.local()


class Stopped(Exception):
    pass


def safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")


def mod_folder(domain, mod_id, mod_name):
    base = DOWNLOAD_DIR / domain
    if base.exists():
        existing = next(base.glob(f"* ({mod_id})"), None)
        if existing:
            return existing
    folder = base / f"{safe_name(mod_name)[:80]} ({mod_id})"
    legacy = next(DOWNLOAD_DIR.glob(f"*_{domain}_{mod_id}"), None)
    if legacy:
        base.mkdir(parents=True, exist_ok=True)
        legacy.rename(folder)
    return folder


def is_archive(path):
    return path.suffix.lower() in ARCHIVE_EXTENSIONS


def extract_dir(folder, f):
    name = f["name"]
    if f.get("version") and f["version"] not in name:
        name = f"{name} {f['version']}"
    return folder / safe_name(name)


def is_extracted(out):
    return out.is_dir() and any(out.iterdir())


def extract_python(archive, out):
    suffix = archive.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(out)
    elif suffix == ".7z":
        with py7zr.SevenZipFile(archive) as sz:
            sz.extractall(out)
    else:
        raise RuntimeError(f"no python extractor for {suffix}")


def extract_7zip(archive, out):
    if not Path(SEVEN_ZIP).exists():
        raise RuntimeError(f"7-Zip not found at {SEVEN_ZIP}")
    result = subprocess.run(
        [SEVEN_ZIP, "x", "-y", "-bso0", "-bsp0", f"-o{out}", str(archive)],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"7-Zip exit code {result.returncode}")


def extract(archive, out, mod_name, task=None):
    if task is not None:
        progress.update(task, status="[magenta]extracting…[/]")
    try:
        try:
            extract_python(archive, out)
        except Exception:
            shutil.rmtree(out, ignore_errors=True)
            extract_7zip(archive, out)
    except Exception as e:
        shutil.rmtree(out, ignore_errors=True)
        record_failure(mod_name, archive.name, f"extract failed: {e}")
        return
    if DELETE_ARCHIVE_AFTER_EXTRACT:
        archive.unlink()
    log("OK", f"{mod_name} | extracted")


def already_downloaded(target, out):
    return target.exists() or (EXTRACT and is_archive(target) and is_extracted(out))


def http_session():
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session(impersonate="chrome")
    return thread_local.session


def download(url, target, out, size, mod_name):
    part = target.with_name(target.name + ".part")
    task = progress.add_task(short(target.name), total=size, status="")
    error = None
    try:
        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            written = 0
            resp = None
            attempt_started = time.time()
            try:
                resp = http_session().get(url, stream=True, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
                if not resp.ok:
                    raise RuntimeError(f"CDN returned HTTP {resp.status_code}")
                expected = int(resp.headers.get("content-length") or 0)
                progress.reset(task, total=expected or size, status="")
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(CHUNK_SIZE):
                        if stop_event.is_set():
                            raise Stopped
                        fh.write(chunk)
                        written += len(chunk)
                        progress.advance(task, len(chunk))
                        with stats_lock:
                            stats["bytes"] += len(chunk)
                if expected and written != expected:
                    raise RuntimeError(f"incomplete download, got {written} of {expected} bytes")
                part.replace(target)
                with stats_lock:
                    stats["downloaded"] += 1
                took = max(time.time() - attempt_started, 0.001)
                log("OK", f"{mod_name} | {size_str(written)} in {hms(took)} ({size_str(written / took)}/s)")
                if EXTRACT and is_archive(target):
                    extract(target, out, mod_name, task)
                return
            except Stopped:
                return
            except Exception as e:
                error = str(e) or type(e).__name__
                with stats_lock:
                    stats["bytes"] -= written
                progress.update(task, status=f"[yellow]retry {attempt}/{DOWNLOAD_RETRIES}: {error[:60]}[/]")
                log("RETRY", f"{mod_name} | attempt {attempt}/{DOWNLOAD_RETRIES} failed: {error}")
                if stop_event.wait(5 * attempt):
                    return
            finally:
                if resp is not None:
                    resp.close()
        with stats_lock:
            stats["todo_bytes"] -= size
        record_failure(mod_name, target.name, f"{error} (after {DOWNLOAD_RETRIES} attempts)")
    finally:
        part.unlink(missing_ok=True)
        progress.remove_task(task)


def acquire(slots):
    while not slots.acquire(timeout=0.25):
        if stop_event.is_set():
            raise Stopped


class ModError(Exception):
    def __init__(self, label, reason):
        super().__init__(reason)
        self.label = label


def file_size(f):
    return f.get("size_in_bytes") or f["size_kb"] * 1024


def scan_mod(domain, mod_id, file_id=None):
    label = f"mod ({mod_id})"
    try:
        info = mod_info(domain, mod_id)
        name = info.get("name") or "unknown mod"
        label = f"{name} ({mod_id})"
        if not info.get("available", True):
            raise RuntimeError(f"mod is {info.get('status', 'unavailable')} on Nexus, cannot be downloaded")
        if file_id is not None:
            files = [mod_file(domain, mod_id, file_id)]
        else:
            files = mod_files(domain, mod_id)
            if not files:
                raise RuntimeError(f"no file in categories {sorted(FILE_CATEGORIES)}")
    except Exception as e:
        raise ModError(label, str(e)) from e
    folder = mod_folder(domain, mod_id, name)
    items = []
    for f in files:
        target = folder / safe_name(f["file_name"])
        out = extract_dir(folder, f)
        items.append((f, target, out, already_downloaded(target, out)))
    return label, folder, items


class Scanner(threading.Thread):
    def __init__(self, mods):
        super().__init__(daemon=True)
        self.mods = mods
        self.results = {}
        self.ready = {(domain, mod_id, file_id): threading.Event() for domain, mod_id, file_id, _ in mods}

    def run(self):
        for domain, mod_id, file_id, _ in self.mods:
            if stop_event.is_set():
                return
            key = (domain, mod_id, file_id)
            try:
                result = scan_mod(domain, mod_id, file_id)
                todo = sum(file_size(f) for f, _, _, done in result[2] if not done)
            except Exception as e:
                result, todo = e, 0
            with stats_lock:
                stats["scanned"] += 1
                stats["todo_bytes"] += todo
            self.results[key] = result
            self.ready[key].set()
            stop_event.wait(SCAN_DELAY_S)

    def get(self, key):
        while not self.ready[key].wait(0.25):
            if stop_event.is_set():
                raise Stopped
        result = self.results[key]
        if isinstance(result, Exception):
            raise result
        return result


class UpNext:
    STYLES = ("grey66", "grey46", "grey30")

    def __init__(self, scanner, mods):
        self.scanner = scanner
        self.mods = mods

    def __rich__(self):
        lines = []
        for domain, mod_id, file_id, _ in self.mods[stats["processed"]:]:
            if len(lines) == len(self.STYLES):
                break
            key = (domain, mod_id, file_id)
            if key in submitted:
                continue
            style = self.STYLES[len(lines)]
            if not self.scanner.ready[key].is_set():
                lines.append(Text(f"{fit(f'mod ({mod_id})')} scanning…", style=style))
                continue
            result = self.scanner.results[key]
            if isinstance(result, Exception):
                continue
            label, _, items = result
            todo = [file_size(f) for f, _, _, done in items if not done]
            if todo:
                lines.append(Text(f"{fit(label)} {size_str(sum(todo)):>10}  up next", style=style))
        return Group(*lines)


submitted = set()


def queue_mod(scanner, domain, mod_id, file_id, link, pool, slots, futures):
    key = (domain, mod_id, file_id)
    try:
        label, folder, items = scanner.get(key)
    except ModError as e:
        record_failure(e.label, link, str(e))
        return
    except Stopped:
        raise
    except Exception as e:
        record_failure(f"mod ({mod_id})", link, str(e))
        return
    for f, target, out, done in items:
        if done:
            with stats_lock:
                stats["skipped"] += 1
            log("SKIP", f"{label} | already downloaded")
            if EXTRACT and target.exists() and is_archive(target) and not is_extracted(out):
                futures.append(pool.submit(extract, target, out, label))
            continue
        acquire(slots)
        try:
            url = generate_download_url(domain, mod_id, f["file_id"])
            folder.mkdir(parents=True, exist_ok=True)
            futures.append(pool.submit(worker, slots, url, target, out, file_size(f), label))
            submitted.add(key)
        except Stopped:
            slots.release()
            raise
        except Exception as e:
            slots.release()
            with stats_lock:
                stats["todo_bytes"] -= file_size(f)
            record_failure(label, link, str(e))
            continue
        if stop_event.wait(REQUEST_DELAY_S):
            raise Stopped


def worker(slots, url, target, out, size, mod_name):
    try:
        download(url, target, out, size, mod_name)
    finally:
        slots.release()


def read_mod_list():
    mods, seen = [], set()
    for line in MOD_LIST.read_text(encoding="utf-8").splitlines():
        hit = re.search(r"nexusmods\.com/(\w+)/mods/(\d+)", line)
        file_hit = re.search(r"[?&]file_id=(\d+)", line)
        if not hit:
            continue
        key = (hit[1], int(hit[2]), int(file_hit[1]) if file_hit else None)
        if key not in seen:
            seen.add(key)
            mods.append((*key, line.strip()))
    return mods


def preflight(mods):
    if not NEXUS_SESSION_COOKIE:
        console.print(
            f"[red]No session cookie set.[/] Log in on nexusmods.com, open the browser dev tools "
            f"(Application → Cookies), copy the value of [bold]nexusmods_session[/] and paste it as "
            f"[bold]session_cookie[/] in {CONFIG_FILE}"
        )
        return False
    if not mods:
        console.print(f"[red]No mods to download.[/] Add one Nexus mod link per line to {MOD_LIST}")
        return False
    return True


def run():
    mods = read_mod_list()
    if not preflight(mods):
        return
    open_log()
    get_token()
    stats["total"] = len(mods)
    log("INFO", f"mod list: {MOD_LIST} ({len(mods)} unique mods)")
    log("INFO", f"output: {DOWNLOAD_DIR}")
    log("INFO", f"settings: {MAX_PARALLEL_DOWNLOADS} parallel, categories {sorted(FILE_CATEGORIES)}, "
                f"extract {'on' if EXTRACT else 'off'}, request delay {REQUEST_DELAY_S}s")

    slots = threading.BoundedSemaphore(MAX_QUEUED_LINKS)
    pool = ThreadPoolExecutor(MAX_PARALLEL_DOWNLOADS)
    futures = []
    scanner = Scanner(mods)
    scanner.start()
    try:
        with Live(view(UpNext(scanner, mods)), console=console, screen=True, refresh_per_second=10):
            try:
                for domain, mod_id, file_id, link in mods:
                    queue_mod(scanner, domain, mod_id, file_id, link, pool, slots, futures)
                    stats["processed"] += 1
                while not all(f.done() for f in futures):
                    time.sleep(0.25)
            except (KeyboardInterrupt, Stopped):
                stop_event.set()
                stats["stopping"] = True
                log("INFO", "stopping, removing partial downloads")
            pool.shutdown(wait=True, cancel_futures=True)
    finally:
        finish()
