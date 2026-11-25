"""Phone inbox for ArchiveDrop: receives archive.org links shared to a private ntfy topic.

Share a link from the phone to the topic (ntfy app > Share). Optional words in the same message:
  a folder name (books, audiobooks, ...) and file types (.pdf .mp3). ArchiveDrop replies on the same
  topic ("Added ...", "Done ...") tagged 'archivedrop' so it never re-reads its own replies.
"""
import json
import re
import secrets
import threading
import time

LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?archive\.org/(?:details|download)/[^\s\"'<>|]+", re.I)
TYPE_RE = re.compile(r"\*?\.[A-Za-z0-9]{1,8}")
REPLY_TAG = "archivedrop"


def new_topic():
    return "archivedrop-" + "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(12))


def parse(text, systems):
    """-> (links, types 'glob,glob', system or '')"""
    text = text or ""
    links = []
    for l in LINK_RE.findall(text):
        l = l.rstrip(").,;")
        if not l.lower().startswith("http"):
            l = "https://" + l
        if l not in links:
            links.append(l)
    rest = LINK_RE.sub(" ", text)
    sysmap = {s.lower(): s for s in systems}
    types, system = [], ""
    for tok in re.split(r"[\s,;]+", rest):
        tok = tok.strip()
        if not tok:
            continue
        if TYPE_RE.fullmatch(tok):
            g = "*" + tok.lstrip("*").lower()
            if g not in types:
                types.append(g)
        elif not system and tok.lower() in sysmap:
            system = sysmap[tok.lower()]
    return links, ",".join(types), system


def _headers(cfg):
    tok = (cfg.get("ntfy_token") or "").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _base(cfg):
    return (cfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/") + "/" + cfg["ntfy_topic"]


def reply(cfg, text, title="ArchiveDrop", tags="inbox_tray"):
    """Post a status message to the phone. Never raises."""
    if not (cfg.get("ntfy_enabled") and cfg.get("ntfy_topic")):
        return
    try:
        import requests
        requests.post(_base(cfg), data=text.encode("utf-8"), timeout=15,
                      headers={**_headers(cfg), "Title": title, "Tags": f"{REPLY_TAG},{tags}"})
    except Exception:
        pass


def _is_own(msg):
    return REPLY_TAG in (msg.get("tags") or [])


def poll(cfg):
    """Messages since cfg['ntfy_since'] (one request; for the background task)."""
    import requests
    since = cfg.get("ntfy_since")
    if not since:
        return []
    r = requests.get(_base(cfg) + "/json", params={"poll": "1", "since": since},
                     headers=_headers(cfg), timeout=30)
    r.raise_for_status()
    out = []
    for line in r.text.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") == "message":
            out.append(m)
    return out


def latest_id(cfg):
    """Id of the newest message on the topic (so enabling doesn't replay old ones)."""
    import requests
    r = requests.get(_base(cfg) + "/json", params={"poll": "1", "since": "all"},
                     headers=_headers(cfg), timeout=30)
    r.raise_for_status()
    last = None
    for line in r.text.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") == "message":
            last = m.get("id")
    return last


class Listener:
    """Keeps a streaming connection to the topic and calls on_message(msg) for each new message."""

    def __init__(self, get_cfg, on_message, set_since, log):
        self.get_cfg, self.on_message, self.set_since, self.log = get_cfg, on_message, set_since, log
        self.status = "off"
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.status = "off"

    POLL_SECONDS = 8   # stays inside ntfy.sh's free limit (1 request per 5 s sustained)

    def _run(self, stop):
        """Short polls instead of a held-open stream: some antivirus/web filters on Windows buffer
        streaming responses indefinitely, while plain requests always get through."""
        import requests
        backoff = self.POLL_SECONDS
        while not stop.is_set():
            cfg = self.get_cfg()
            if not (cfg.get("ntfy_enabled") and cfg.get("ntfy_topic")):
                self.status = "off"
                return
            if self.status == "off":
                self.status = "connecting"
            since = cfg.get("ntfy_since") or str(int(time.time()))
            if not cfg.get("ntfy_since"):
                self.set_since(since)
            try:
                r = requests.get(_base(cfg) + "/json", params={"poll": "1", "since": since},
                                 headers=_headers(cfg), timeout=20)
                r.raise_for_status()
                self.status = "on"
                backoff = self.POLL_SECONDS
                for line in r.text.splitlines():
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("event") != "message":
                        continue
                    self.set_since(m.get("id"))
                    if not _is_own(m):
                        try:
                            self.on_message(m)
                        except Exception as e:
                            self.log(f"PHONE: couldn't handle message: {e}")
            except Exception as e:
                if stop.is_set():
                    return
                if self.status != "offline":
                    self.log(f"PHONE: can't reach ntfy ({e.__class__.__name__}), will keep trying")
                self.status = "offline"
                backoff = min(backoff * 2, 120)
            if stop.wait(backoff):
                return


def qr_svg(data):
    """SVG markup for a QR code (empty string if the qrcode package isn't available)."""
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        s = img.to_string()
        return s.decode("utf-8") if isinstance(s, bytes) else s
    except Exception:
        return ""
