import time

from auth import USER_AGENT, get_token, http_client
from config import (
    ALL_FILES_PER_CATEGORY, API_URL, APP_VERSION, DOWNLOAD_URL_ENDPOINT, FALLBACK_CATEGORIES, FILE_CATEGORIES,
    NEXUS_SESSION_COOKIE, PROTOCOL_VERSION, RATE_LIMIT_WAIT_S,
)
from ui import log


class ApiError(Exception):
    pass


def api_get(path):
    while True:
        resp = http_client().get(f"{API_URL}{path}", headers={
            "Authorization": f"Bearer {get_token()}",
            "Application-Name": "Vortex",
            "Application-Version": APP_VERSION,
            "Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": USER_AGENT,
        })
        if resp.status_code == 429:
            log("INFO", f"API rate limit hit, waiting {RATE_LIMIT_WAIT_S}s")
            time.sleep(RATE_LIMIT_WAIT_S)
            continue
        if not resp.ok:
            try:
                message = resp.json().get("message", "")
            except ValueError:
                message = resp.text[:200]
            raise ApiError(f"API {resp.status_code}: {message}")
        return resp.json()


game_ids = {}


def game_id(domain):
    if domain not in game_ids:
        game_ids[domain] = api_get(f"/games/{domain}.json")["id"]
    return game_ids[domain]


def mod_info(domain, mod_id):
    return api_get(f"/games/{domain}/mods/{mod_id}.json")


def mod_file(domain, mod_id, file_id):
    return api_get(f"/games/{domain}/mods/{mod_id}/files/{file_id}.json")


def mod_files(domain, mod_id):
    all_files = api_get(f"/games/{domain}/mods/{mod_id}/files.json")["files"]
    files = [f for f in all_files if f["category_name"] in FILE_CATEGORIES]
    for category in FALLBACK_CATEGORIES:
        if files:
            break
        files = [f for f in all_files if f["category_name"] == category]
        if files:
            log("INFO", f"mod {mod_id}: no {'/'.join(sorted(FILE_CATEGORIES))} file, using {category}")
    if ALL_FILES_PER_CATEGORY:
        return files
    primary = [f for f in files if f["is_primary"]]
    return primary or sorted(files, key=lambda f: f["uploaded_timestamp"])[-1:]


def generate_download_url(domain, mod_id, file_id):
    resp = http_client().post(
        DOWNLOAD_URL_ENDPOINT,
        data={"game_id": game_id(domain), "fid": file_id, "collection_id": 0},
        cookies={"nexusmods_session": NEXUS_SESSION_COOKIE},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.nexusmods.com",
            "Referer": f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files&file_id={file_id}",
        },
    )
    if not resp.ok:
        raise RuntimeError(f"download link request failed: HTTP {resp.status_code}")
    try:
        url = resp.json().get("url") if resp.text.startswith("{") else None
    except ValueError:
        url = None
    if not url:
        raise RuntimeError("no download link returned, session cookie probably expired")
    return url
