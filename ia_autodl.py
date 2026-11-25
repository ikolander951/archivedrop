"""
IA AutoDL - automatic Internet Archive downloader.

queue.txt  -> one-shot downloads. Lines are moved to done.txt / failed.txt when finished.
watch.txt  -> kept in sync. Re-checked every `watch_interval_hours`; only new/changed files download.

Line format (everything after the first field is optional):
    <identifier | archive.org URL | search: <query>>  |  <glob(s), comma-separated>  |  <subfolder>

Examples:
    https://archive.org/details/some-item
    some-item | *.pdf,*.epub | books
    https://archive.org/download/some-item/Folder/One%20File.zip
    search: collection:some-collection AND mediatype:software | *.zip | MyCollection

Lines starting with # are ignored.
"""
import fnmatch
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

def _portable_dir():
    """Portable copy: a portable.txt next to ArchiveDrop.exe keeps all data in a 'data' folder beside it."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "portable.txt").exists():
            return exe_dir / "data"
    return None


PORTABLE = _portable_dir() is not None


def _data_home():
    """Script copy: data next to the script. Portable copy: data\\ next to the exe.
    Installed (frozen) app: %LOCALAPPDATA%\\ArchiveDrop."""
    custom = os.environ.get("ARCHIVEDROP_DATA")
    if not custom and PORTABLE:
        d = _portable_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d
    if custom or getattr(sys, "frozen", False):
        d = Path(custom) if custom else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ArchiveDrop"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(__file__).resolve().parent


def _default_download_dir():
    return str(Path.home() / "Downloads" / "InternetArchive")


HOME = _data_home()
CONFIG_FILE = HOME / "config.json"
QUEUE = HOME / "queue.txt"
WATCH = HOME / "watch.txt"
DONE = HOME / "done.txt"
IA_CONFIG = HOME / "ia.ini"          # archive.org access keys after signing in (never the password)


ACCOUNT_FILE = HOME / "account.json"
ANON_CONFIG = HOME / "ia-anonymous.ini"   # empty; used while signed out


def _read_ini():
    import configparser
    raw = IA_CONFIG.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    c = configparser.RawConfigParser()
    c.read_string(text)
    return c


def _write_ini(c, display):
    """Write ia.ini in plain ASCII (readable whatever encoding the library assumes) and keep the name as
    archive.org shows it in account.json. Empty or "None" cookie lines are dropped."""
    if c.has_section("cookies") and not all(v and v != "None" for _, v in c.items("cookies")):
        c.remove_section("cookies")
    for s in c.sections():
        for k, v in c.items(s):
            c.set(s, k, (v or "").encode("ascii", "replace").decode("ascii"))
    with open(IA_CONFIG, "w", encoding="ascii") as fh:
        c.write(fh)
    ACCOUNT_FILE.write_text(json.dumps({"name": display or ""}), encoding="utf-8")


def _use_account():
    """Point the internetarchive library at our sign-in file when there is one."""
    if IA_CONFIG.exists():
        try:                                     # tidy files written by 1.6.0 to 1.8.1
            raw = IA_CONFIG.read_bytes()
            if any(b > 127 for b in raw) or b"= None" in raw:
                c = _read_ini()
                _write_ini(c, c.get("general", "screenname", fallback=""))
        except Exception:
            pass
        os.environ["IA_CONFIG_FILE"] = str(IA_CONFIG)
    else:
        # Signed out: use an empty file so the library never falls back to a leftover
        # ~/.config/internetarchive/ia.ini (old ones in the Windows code page crash Python 3.15+).
        try:
            if not ANON_CONFIG.exists():
                ANON_CONFIG.write_text("", encoding="ascii")
            os.environ["IA_CONFIG_FILE"] = str(ANON_CONFIG)
        except OSError:
            if os.environ.get("IA_CONFIG_FILE") == str(IA_CONFIG):
                del os.environ["IA_CONFIG_FILE"]


_use_account()


def account():
    """The signed-in archive.org screen name, or '' when not signed in."""
    if not IA_CONFIG.exists():
        return ""
    try:
        name = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8")).get("name")
        if name:
            return name
    except Exception:
        pass
    try:
        return _read_ini().get("general", "screenname", fallback="") or "your archive.org account"
    except Exception:
        return "your archive.org account"


def sign_in(email, password):
    """Sign in to archive.org. Only the access keys and cookies it returns are saved, in ia.ini."""
    import configparser
    from internetarchive.config import get_auth_config
    from internetarchive.exceptions import AuthenticationError
    email = (email or "").strip()
    if not email or not password:
        raise ValueError("Enter the email and password for your archive.org account.")
    try:
        ac = get_auth_config(email, password)
    except AuthenticationError as e:
        raise ValueError(str(e)) from None
    except Exception as e:
        raise ValueError(f"Couldn't reach archive.org to sign in ({e.__class__.__name__}). Try again.") from None
    c = configparser.RawConfigParser()
    c.read_dict({"s3": ac["s3"], "cookies": ac["cookies"], "general": {"screenname": ac["general"]["screenname"]}})
    _write_ini(c, ac["general"]["screenname"])
    _use_account()
    return account()


KEYS_PAGE = "https://archive.org/account/s3.php"


def sign_in_keys(access, secret):
    """Sign in with the access keys from archive.org's keys page. Works for every account, including ones
    created with Google, which have no archive.org password to type here."""
    from internetarchive.api import get_user_info
    import configparser
    access, secret = (access or "").strip(), (secret or "").strip()
    if not access or not secret:
        raise ValueError("Paste both keys from your archive.org keys page.")
    try:
        info = get_user_info(access, secret)
    except Exception as e:
        if e.__class__.__name__ == "AuthenticationError":
            raise ValueError("archive.org didn't accept those keys. Copy them again from the keys page.") from None
        raise ValueError(f"Couldn't check those keys with archive.org ({e.__class__.__name__}). Try again.") from None
    name = (info or {}).get("screenname") or (info or {}).get("username") or ""
    if not name:
        raise ValueError("archive.org didn't accept those keys. Copy them again from the keys page.")
    c = configparser.RawConfigParser()
    c.read_dict({"s3": {"access": access, "secret": secret}, "general": {"screenname": name}})
    _write_ini(c, name)
    _use_account()
    return account()


def sign_out():
    for f in (IA_CONFIG, ACCOUNT_FILE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    _use_account()


def _access_hint(f):
    """After a failed download, say so plainly when the file needs an account or different access."""
    try:
        r = f.item.session.head(f.url, allow_redirects=True, timeout=30)
        if r.status_code in (401, 403):
            if account():
                return "your archive.org account doesn't have access to this file"
            return "this file needs an archive.org account. Sign in from Settings, then try again"
    except Exception:
        pass
    return ""
FAILED = HOME / "failed.txt"
STATE = HOME / "state.json"
LOCK = HOME / "ia_autodl.lock"
LOGDIR = HOME / "logs"

DEFAULT_CONFIG = {
    "download_dir": _default_download_dir(),
    "originals_only": True,          # skip IA-generated derivatives (thumbnails, ocr, mp3 transcodes...)
    "skip_ia_metadata": True,        # skip _meta.xml, _files.xml, .torrent, .sqlite etc.
    "max_attempts": 5,               # queue line moves to failed.txt after this many failed runs
    "watch_interval_hours": 24,
    "retries_per_file": 10,
    "lock_stale_hours": 12,
    "unsorted_dir": str(Path.home() / "Downloads" / "InternetArchive"),  # lines with no folder
    "item_folder_in_subfolder": False,
    "parallel": 3,                   # files downloading at the same time (1-6)
    "ntfy_enabled": False,           # phone links via ntfy (see ntfy_inbox.py)
    "ntfy_topic": "",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_token": "",
    "ntfy_since": None,
    "ntfy_last": None,               # last phone links (for follow-up folder-name messages)
    "auto_update_check": True,       # look for a newer GitHub release at startup and every 6 hours
    "verify_checksums": True,        # check every downloaded file against the archive's MD5
    "speed_limit_kbps": 0,           # total download speed cap across all files, KB/s (0 = no limit)
    "schedule_enabled": False,       # only download between schedule_start and schedule_end
    "schedule_start": "01:00",
    "schedule_end": "07:00",
}

IA_JUNK = ("_meta.xml", "_files.xml", "_meta.sqlite", "_archive.torrent",
           "_reviews.xml", "__ia_thumb.jpg", "_rules.conf")
INVALID_CHARS = re.compile(r'[<>:"|?*]')


# ---------- small helpers ----------
LOG_HOOK = None  # set by the GUI to receive log lines
_LOG_LOCK = __import__("threading").Lock()


def log(msg):
    LOGDIR.mkdir(exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if LOG_HOOK:
        LOG_HOOK(line)
    else:
        print(line, flush=True)
    with _LOG_LOCK, open(LOGDIR / f"{datetime.now():%Y-%m}.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_lines(path):
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8-sig").splitlines()


def append_line(path, text):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def safe_rel(name):
    """IA file names can contain sub-folders; strip anything Windows can't store."""
    parts = [INVALID_CHARS.sub("_", p).rstrip(" .") for p in name.replace("\\", "/").split("/")]
    parts = [p for p in parts if p not in ("", ".", "..")]
    return Path(*parts) if parts else Path("_unnamed")


# ---------- parsing ----------
def parse_line(raw):
    """Return dict(kind, target, file, globs, subfolder) or None for blank/comment lines."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    fields = [f.strip() for f in line.split("|")]
    head = fields[0]
    globs = [g.strip() for g in fields[1].split(",") if g.strip()] if len(fields) > 1 else []
    sub = fields[2] if len(fields) > 2 and fields[2] else None

    if head.lower().startswith("search:"):
        return dict(kind="search", target=head[7:].strip(), file=None, globs=globs, subfolder=sub)

    single_file = None
    if "archive.org" in head:
        url = head if "://" in head else "https://" + head
        path = [unquote(p) for p in urlparse(url).path.split("/") if p]
        if len(path) >= 2 and path[0] in ("details", "download", "metadata", "compress"):
            ident = path[1]
            if path[0] == "download" and len(path) > 2:
                single_file = "/".join(path[2:])
        else:
            raise ValueError(f"Can't find an item identifier in URL: {head}")
    else:
        ident = head
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", ident):
        raise ValueError(f"Not an archive.org link or item identifier: {head}")
    return dict(kind="item", target=ident, file=single_file, globs=globs, subfolder=sub)


def wanted(f, spec, cfg):
    name = f.name
    if spec["file"]:
        return name == spec["file"]
    low = name.lower()
    if cfg["skip_ia_metadata"] and (low.endswith(IA_JUNK) or low.endswith(".torrent")):
        return False
    if cfg["originals_only"] and getattr(f, "source", "original") not in (None, "original"):
        return False
    if spec["globs"]:
        base = name.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatch(low, g.lower()) or fnmatch.fnmatch(base.lower(), g.lower())
                   for g in spec["globs"])
    return True


# ---------- downloading ----------
class Stopped(RuntimeError):
    pass


def _within(base, target):
    """Race-free stand-in for internetarchive's path-traversal check. The library's version calls
    realpath() on a file that doesn't exist yet, which on Windows briefly misresolves while other
    files in the same folder are being written (parallel downloads). abspath still collapses '..',
    so anything escaping the download folder is still refused."""
    b = os.path.normcase(os.path.abspath(base)).rstrip("\\/") + os.sep
    return os.path.normcase(os.path.abspath(target)).startswith(b)


def _patch_ia_guard():
    from internetarchive import utils as ia_utils
    if getattr(ia_utils.is_path_within_directory, "__name__", "") != "_within":
        ia_utils.is_path_within_directory = _within


# ---------- speed limit and download hours ----------
# Shared by every download thread, so the cap is the total speed. The app updates it live from Settings.
LIMITS = {"kbps": 0, "schedule": False, "start": "01:00", "end": "07:00"}
_TLS = threading.local()             # marks the threads that are inside a file download
_RATE_LOCK = threading.Lock()
_RATE = {"next": 0.0}


def apply_limits(cfg):
    LIMITS.update(kbps=max(0, int(cfg.get("speed_limit_kbps") or 0)),
                  schedule=bool(cfg.get("schedule_enabled")),
                  start=valid_hhmm(cfg.get("schedule_start"), "01:00"),
                  end=valid_hhmm(cfg.get("schedule_end"), "07:00"))


def valid_hhmm(v, default):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(v or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return default
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(now=None):
    """True when downloads are allowed right now (always, unless download hours are on)."""
    if not LIMITS["schedule"]:
        return True
    a, b = _mins(LIMITS["start"]), _mins(LIMITS["end"])
    if a == b:
        return True
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    return a <= cur < b if a < b else (cur >= a or cur < b)


def hold_text():
    """'' while downloads are allowed, otherwise a short line saying when they start."""
    return "" if in_window() else f"Waiting for download hours ({LIMITS['start']} to {LIMITS['end']})"


HANDOVER = False                     # set by the background task: give way when the window opens
_GUI = {"t": -10.0, "open": False}


def _gui_open():
    """Is the ArchiveDrop window running? Checked at most every 2 seconds."""
    now = time.monotonic()
    if now - _GUI["t"] >= 2:
        _GUI["t"] = now
        try:
            pid = int((HOME / "gui.pid").read_text().strip())
            _GUI["open"] = pid != os.getpid() and pid_alive(pid)
        except (OSError, ValueError):
            _GUI["open"] = False
    return _GUI["open"]


def _hold(stop):
    """Pause until download hours. Without a stop event (scheduled task) end the run instead,
    so the next scheduled run picks up where this one left off. The scheduled task also ends,
    mid-file if need be, as soon as the window opens: the window resumes the partial files."""
    if stop is None and HANDOVER and _gui_open():
        raise Stopped("Handed over to the ArchiveDrop window")
    while not in_window():
        if stop is None:
            raise Stopped("Outside download hours (will resume where it left off)")
        if stop.wait(5):
            raise Stopped("Stopped by user (will resume where it left off)")


def _throttle(n, stop):
    if stop is not None and stop.is_set():      # Stop and closing the window act mid-file; partial files resume
        raise Stopped("Stopped by user (will resume where it left off)")
    _hold(stop)
    rate = LIMITS["kbps"] * 1024
    if rate <= 0:
        return
    with _RATE_LOCK:
        now = time.monotonic()
        start = max(now, _RATE["next"])
        _RATE["next"] = start + n / rate
        delay = _RATE["next"] - now - 1.0      # allow about a second of burst
    while delay > 0:
        if stop is not None and stop.is_set():
            raise Stopped("Stopped by user (will resume where it left off)")
        step = min(delay, 0.5)
        time.sleep(step)
        delay -= step


def _install_gate():
    """Route the body of file downloads through _throttle. Only threads inside fetch_file are
    affected; metadata lookups, update checks and phone messages are never slowed down."""
    import requests
    R = requests.models.Response
    if getattr(R.iter_content, "_archivedrop", False):
        return
    orig = R.iter_content

    def iter_content(self, chunk_size=1, decode_unicode=False):
        it = orig(self, chunk_size, decode_unicode)
        if not getattr(_TLS, "active", False):
            return it
        stop = getattr(_TLS, "stop", None)

        def gated():
            for c in it:
                if not c:
                    continue
                if LIMITS["kbps"] <= 0 or len(c) <= 65536:
                    _throttle(len(c), stop)
                    yield c
                    continue
                for k in range(0, len(c), 65536):      # small slices keep progress bars moving when capped
                    piece = c[k:k + 65536]
                    _throttle(len(piece), stop)
                    yield piece
        return gated()

    iter_content._archivedrop = True
    R.iter_content = iter_content


def md5_file(path, on_progress=None):
    h, done = hashlib.md5(), 0
    with open(path, "rb") as fh:
        while True:
            b = fh.read(4 << 20)
            if not b:
                break
            h.update(b)
            done += len(b)
            if on_progress:
                on_progress(done)
    return h.hexdigest()


def plan_item(ident, spec, cfg, dest_root):
    """Look the item up and work out what still needs downloading.
    Returns (dest, todo, skipped, all_files) where todo is a list of (file, target_path, size)."""
    from internetarchive import get_item
    from internetarchive import utils as ia_utils
    item = get_item(ident, request_kwargs={"timeout": 60})
    if not item.exists:
        raise RuntimeError(f"Item '{ident}' not found (or dark/removed)")
    if item.metadata.get("access-restricted-item") in (True, "true"):
        log(f"  note: {ident} is access-restricted; some files may need an archive.org sign-in (Settings)")
    files = [f for f in item.get_files() if wanted(f, spec, cfg)]
    if not files:
        raise RuntimeError(f"No files in '{ident}' matched {spec['globs'] or spec['file'] or 'filters'}")

    if spec["subfolder"]:
        # Subfolder = a folder under download_dir (e.g. books): files go straight in, no per-item folder.
        # A full path (C:\\...\\books) is accepted too.
        sub = Path(os.path.expandvars(spec["subfolder"]))
        dest = sub if sub.is_absolute() else dest_root / safe_rel(spec["subfolder"])
        if not dest.exists():
            log(f"  note: creating new folder {dest} - check the folder name if that's a typo")
        if cfg.get("item_folder_in_subfolder"):
            dest = dest / safe_rel(ident)
    else:
        unsorted = cfg.get("unsorted_dir") or str(dest_root)
        dest = Path(os.path.expandvars(unsorted)) / safe_rel(ident)
    dest.mkdir(parents=True, exist_ok=True)

    todo, skipped = [], 0
    for f in files:
        # Let the library pick the on-disk name (it sanitizes Windows-illegal chars itself)
        rel = ia_utils.sanitize_windows_relpath(f.name)[0] if os.name == "nt" else f.name
        target = dest / rel
        size = int(f.size) if getattr(f, "size", None) not in (None, "") else None
        if target.exists() and size is not None and target.stat().st_size == size:
            skipped += 1
        else:
            todo.append((f, target, size))
    return dest, todo, skipped, files


def fetch_file(f, dest, target, size, cfg, on_phase=None):
    """Download one file into dest (resumes a partial file), then check it against the archive's MD5.
    Returns "verified", or "unchecked" when verifying is off or the archive lists no checksum.
    Raises on failure; a file that fails the checksum is deleted so the next try fetches it fresh."""
    stop = cfg.get("stop_event")
    if stop is not None and stop.is_set():
        raise Stopped("Stopped by user (will resume where it left off)")
    _hold(stop)
    log(f"  -> {f.name} ({(size or 0) / 1e9:.2f} GB)")
    # Create the file's folder first: the library's path-safety check resolves real paths, and with
    # parallel downloads another thread creating the same sub-folder mid-check can make it misfire.
    target.parent.mkdir(parents=True, exist_ok=True)
    _patch_ia_guard()
    _install_gate()
    # Restricted files: one quick check up front instead of ten slow retries ending in "forbidden"
    if getattr(f, "private", None) in (True, "true") or f.item.metadata.get("access-restricted-item") in (True, "true"):
        hint = _access_hint(f)
        if hint:
            raise IOError(hint)
    _TLS.active, _TLS.stop = True, stop
    try:
        for attempt in (1, 2):
            try:
                ok = f.download(file_path=f.name, destdir=str(dest), retries=cfg["retries_per_file"],
                                ignore_existing=False, verbose=bool(cfg.get("show_progress")), timeout=120)
                break
            except Stopped:
                raise
            except Exception as e:
                if attempt == 1 and "outside destination" in str(e):
                    time.sleep(0.5)
                    continue
                hint = _access_hint(f)
                if hint:
                    raise IOError(hint) from None
                raise
    finally:
        _TLS.active, _TLS.stop = False, None
    if ok is False:
        raise IOError(_access_hint(f) or "download returned failure")
    if size is not None and target.stat().st_size != size:
        raise IOError(f"size mismatch {target.stat().st_size} != {size}")
    md5 = getattr(f, "md5", None)
    if not cfg.get("verify_checksums", True) or not md5:
        return "unchecked"
    if on_phase:
        on_phase("verifying", 0)
    got = md5_file(target, (lambda n: on_phase("verifying", n)) if on_phase else None)
    if got.lower() != str(md5).strip().lower():
        try:
            target.unlink()
        except OSError:
            pass
        log(f"  !! {f.name}: checksum mismatch (got {got}, archive lists {md5}); deleted")
        raise IOError("checksum didn't match the archive's, so the file was deleted and will download again")
    return "verified"


def download_item(ident, spec, cfg, dest_root):
    dest, todo, skipped, files = plan_item(ident, spec, cfg, dest_root)
    if cfg.get("on_item"):
        cfg["on_item"](ident, dest, files)
    parallel = max(1, min(8, int(cfg.get("parallel") or 1)))
    errors, got = [], 0

    def one(idx, job):
        f, target, size = job
        if cfg.get("on_file"):
            cfg["on_file"](idx, len(todo), f.name, target, size)
        fetch_file(f, dest, target, size, cfg)

    if parallel == 1 or len(todo) <= 1:
        for idx, job in enumerate(todo, 1):
            try:
                one(idx, job)
                got += 1
            except Stopped:
                raise
            except Exception as e:  # keep going, report at the end
                errors.append(f"{job[0].name}: {e}")
                log(f"  !! {job[0].name}: {e}")
    else:
        from concurrent.futures import ThreadPoolExecutor
        stopped = None
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futs = {pool.submit(one, i, job): job for i, job in enumerate(todo, 1)}
            for fut, job in futs.items():
                try:
                    fut.result()
                    got += 1
                except Stopped as e:
                    stopped = stopped or e
                except Exception as e:
                    errors.append(f"{job[0].name}: {e}")
                    log(f"  !! {job[0].name}: {e}")
        if stopped:
            raise stopped
    log(f"  {ident}: {got} downloaded, {skipped} already present, {len(errors)} failed")
    if errors:
        raise RuntimeError(f"{len(errors)} file(s) failed in {ident}")


def process(spec, cfg):
    dest_root = Path(os.path.expandvars(cfg["download_dir"]))
    if spec["kind"] == "item":
        download_item(spec["target"], spec, cfg, dest_root)
        return
    from internetarchive import search_items
    idents = [r["identifier"] for r in search_items(spec["target"], fields=["identifier"])]
    log(f"  search matched {len(idents)} item(s)")
    failures = 0
    for ident in idents:
        try:
            download_item(ident, spec, cfg, dest_root)
        except Stopped:
            raise
        except Exception as e:
            failures += 1
            log(f"  !! {ident}: {e}")
    if failures:
        raise RuntimeError(f"{failures}/{len(idents)} items had failures")


# ---------- runs ----------
def run_queue(cfg, state):
    lines = read_lines(QUEUE)
    attempts = state.setdefault("attempts", {})
    for raw in lines:
        try:
            spec = parse_line(raw)
        except ValueError as e:
            log(f"Bad queue line moved to failed.txt: {e}")
            finish_queue_line(raw, FAILED, f"bad line: {e}")
            continue
        if spec is None:
            continue
        key = raw.strip()
        log(f"QUEUE: {key}")
        try:
            process(spec, cfg)
            finish_queue_line(raw, DONE, "ok")
            attempts.pop(key, None)
        except Stopped as e:              # not a failure: the line stays queued for the next run
            log(f"  {e}")
            break
        except Exception as e:
            attempts[key] = attempts.get(key, 0) + 1
            log(f"  attempt {attempts[key]}/{cfg['max_attempts']} failed: {e}")
            if attempts[key] >= cfg["max_attempts"]:
                finish_queue_line(raw, FAILED, str(e))
                attempts.pop(key, None)
        save_json(STATE, state)


def finish_queue_line(raw, dest_file, note):
    """Remove the line from queue.txt (re-read, so lines added mid-run survive) and log it."""
    current = read_lines(QUEUE)
    if raw in current:
        current.remove(raw)
        QUEUE.write_text("\n".join(current) + ("\n" if current else ""), encoding="utf-8")
    append_line(dest_file, f"{datetime.now():%Y-%m-%d %H:%M}\t{raw.strip()}\t{note}")


def run_watch(cfg, state, force=False):
    last = state.get("last_watch")
    if not force and last and datetime.now() - datetime.fromisoformat(last) < timedelta(hours=cfg["watch_interval_hours"]):
        return
    specs = []
    for raw in read_lines(WATCH):
        try:
            spec = parse_line(raw)
        except ValueError as e:
            log(f"Bad watch line skipped: {e}")
            continue
        if spec:
            specs.append((raw.strip(), spec))
    for key, spec in specs:
        log(f"WATCH: {key}")
        try:
            process(spec, cfg)
        except Stopped as e:              # try the whole sync again next run
            log(f"  {e}")
            return
        except Exception as e:
            log(f"  watch sync incomplete: {e}")
    state["last_watch"] = datetime.now().isoformat(timespec="seconds")
    save_json(STATE, state)


def acquire_lock(cfg):
    if LOCK.exists():
        age_h = (time.time() - LOCK.stat().st_mtime) / 3600
        if age_h < cfg["lock_stale_hours"]:
            try:
                pid = int(LOCK.read_text().strip())
                if pid_alive(pid):
                    return False
            except Exception:
                return False
        LOCK.unlink(missing_ok=True)
    LOCK.write_text(str(os.getpid()))
    return True


def pid_alive(pid):
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def folder_choices(cfg):
    root = Path(os.path.expandvars(cfg["download_dir"]))
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir())
    except Exception:
        return []


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return "q"


def interactive(cfg):
    """One-window flow: paste link -> pick types/folder -> download now with progress."""
    import difflib
    cfg["show_progress"] = True
    systems = folder_choices(cfg)
    print("=" * 70)
    print(" IA AutoDL - paste an archive.org link and it downloads right now")
    print(f" Files go to {cfg['download_dir']}\\<folder>; no folder = {cfg.get('unsorted_dir') or cfg['download_dir']}")
    print(" Empty link = just run the queue.   q = quit")
    print("=" * 70)
    while True:
        link = ask("\nLink: ").strip('"')
        if link.lower() in ("q", "quit", "exit"):
            break
        if link:
            if "|" in link:                       # whole line pasted in one go
                line = link
            else:
                types = ask("File types (Enter = everything, e.g. *.pdf,*.mp3): ")
                system = ""
                while True:
                    system = ask("Folder (Enter = none, e.g. books, audiobooks): ").strip('"')
                    if not system or Path(system).is_absolute() or system in systems or not systems:
                        break
                    close = difflib.get_close_matches(system.lower(), systems, n=3, cutoff=0.5)
                    hint = f" Did you mean: {', '.join(close)}?" if close else ""
                    if ask(f"'{system}' isn't an existing folder.{hint} Create it anyway? (y/N): ").lower() == "y":
                        break
                line = " | ".join([link, types, system]).rstrip(" |")
            try:
                parse_line(line)
            except ValueError as e:
                print(f"  !! {e}")
                continue
            append_line(QUEUE, line)
            print(f"  queued: {line}")
        waited = False
        while not acquire_lock(cfg):
            if not waited:
                print("  A background download is running - waiting for it to finish...")
                waited = True
            time.sleep(10)
        try:
            state = load_json(STATE, {})
            run_queue(cfg, state)
        finally:
            LOCK.unlink(missing_ok=True)
        print("\nDone. Results are in done.txt / failed.txt. Paste another link, or q to quit.")


def main():
    cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}
    if not CONFIG_FILE.exists():
        save_json(CONFIG_FILE, cfg)
    for p in (QUEUE, WATCH):
        p.touch(exist_ok=True)
    if "--interactive" in sys.argv:
        try:
            interactive(cfg)
        except KeyboardInterrupt:
            pass
        except Exception:
            log("FATAL:\n" + traceback.format_exc())
            ask("Something went wrong (details in logs). Press Enter to close.")
        return
    apply_limits(cfg)
    if not in_window():
        return                            # outside download hours: the next scheduled run tries again
    if not acquire_lock(cfg):
        print("Another run is in progress; exiting.")
        return
    try:
        state = load_json(STATE, {})
        run_queue(cfg, state)
        run_watch(cfg, state, force="--sync-now" in sys.argv)
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
