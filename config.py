import shutil
import sys
import tomllib
from pathlib import Path

if getattr(sys, "frozen", False):
    # Built .exe: keep config, mod list, downloads and logs next to the .exe,
    # not in the temporary folder PyInstaller unpacks itself to.
    BASE_DIR = Path(sys.executable).parent
    BUNDLE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = BUNDLE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.toml"

if not CONFIG_FILE.exists():
    example = BASE_DIR / "config.example.toml"
    shutil.copy(example if example.exists() else BUNDLE_DIR / "config.example.toml", CONFIG_FILE)

with open(CONFIG_FILE, "rb") as fh:
    _cfg = tomllib.load(fh)

NEXUS_SESSION_COOKIE = _cfg["nexus"]["session_cookie"]
FILE_CATEGORIES = set(_cfg["nexus"].get("file_categories", ["MAIN"]))
FALLBACK_CATEGORIES = _cfg["nexus"].get("fallback_categories", ["UPDATE", "OPTIONAL", "MISCELLANEOUS"])
ALL_FILES_PER_CATEGORY = _cfg["nexus"].get("all_files_per_category", False)

MOD_LIST = BASE_DIR / _cfg["paths"].get("mod_list", "mods.txt")
DOWNLOAD_DIR = BASE_DIR / _cfg["paths"].get("download_dir", "downloads")
LOG_DIR = BASE_DIR / _cfg["paths"].get("log_dir", "logs")
SESSION_FILE = Path.home() / ".nexus_session.json"

if not MOD_LIST.exists():
    MOD_LIST.write_text("", encoding="utf-8")

MAX_PARALLEL_DOWNLOADS = _cfg["download"].get("max_parallel", 2)
MAX_QUEUED_LINKS = _cfg["download"].get("max_queued_links", 3)
REQUEST_DELAY_S = _cfg["download"].get("request_delay_s", 4.0)
SCAN_DELAY_S = _cfg["download"].get("scan_delay_s", 0.2)
DOWNLOAD_RETRIES = _cfg["download"].get("retries", 3)
RATE_LIMIT_WAIT_S = _cfg["download"].get("rate_limit_wait_s", 60)
CONNECT_TIMEOUT_S = _cfg["download"].get("connect_timeout_s", 15)
READ_TIMEOUT_S = _cfg["download"].get("read_timeout_s", 120)
CHUNK_SIZE = 1024 * 1024

EXTRACT = _cfg["extract"].get("enabled", False)
DELETE_ARCHIVE_AFTER_EXTRACT = _cfg["extract"].get("delete_archive_after", False)
SEVEN_ZIP = _cfg["extract"].get("seven_zip", "C:/Program Files/7-Zip/7z.exe")
ARCHIVE_EXTENSIONS = {".7z", ".zip", ".rar"}

LOG_LINES = _cfg["ui"].get("log_lines", 12)
NAME_WIDTH = _cfg["ui"].get("name_width", 45)

OAUTH_URL = "https://users.nexusmods.com/oauth"
API_URL = "https://api.nexusmods.com/v1"
DOWNLOAD_URL_ENDPOINT = "https://www.nexusmods.com/Core/Libs/Common/Managers/Downloads?GenerateDownloadUrl"
CLIENT_ID = "vortex_loopback"
APP_VERSION = "2.8.2"
PROTOCOL_VERSION = "1.7.3"
