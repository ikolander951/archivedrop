# Clean demo screenshots for the README/site: throwaway data folder, public-domain items, dummy phone topic.
import os, sys, time, json, tempfile, shutil, threading, ctypes, importlib.machinery
from ctypes import wintypes as w
data = tempfile.mkdtemp(prefix="adshots_"); os.environ["ARCHIVEDROP_DATA"] = data
DEMO = r"D:\ArchiveDrop"          # neutral paths for public screenshots (no user name)
assert not os.path.exists(DEMO), "demo folder already exists"
lib = os.path.join(DEMO, "Library")
for d in ("audiobooks", "books", "comics", "music", "software", "videos"):
    os.makedirs(os.path.join(lib, d))
json.dump({"download_dir": lib, "unsorted_dir": os.path.join(DEMO, "Downloads"), "parallel": 3}, open(os.path.join(data, "config.json"), "w"))
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root (this file lives in tools/)
sys.path.insert(0, here); sys.argv = [sys.argv[0]]
import importlib.util
_ldr = importlib.machinery.SourceFileLoader("ad", os.path.join(here, "archivedrop.pyw"))   # load_module() is gone in 3.15
_spec = importlib.util.spec_from_loader("ad", _ldr); ad = importlib.util.module_from_spec(_spec)
sys.modules["ad"] = ad; _ldr.exec_module(ad)
import webview
from PIL import Image
OUT = os.path.join(here, "docs", "screenshots"); os.makedirs(OUT, exist_ok=True)
u, g = ctypes.windll.user32, ctypes.windll.gdi32

def hwnd():
    found = []
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def each(h, _):
        pid = ctypes.c_ulong(); u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
        b = ctypes.create_unicode_buffer(64); u.GetWindowTextW(ctypes.c_void_p(h), b, 64)
        if pid.value == os.getpid() and b.value == "ArchiveDrop" and u.IsWindowVisible(ctypes.c_void_p(h)): found.append(h)
        return True
    u.EnumWindows(each, None); return found[0]

def shot(name):
    h = ctypes.c_void_p(hwnd()); r = w.RECT(); u.GetWindowRect(h, ctypes.byref(r))
    W, H = r.right - r.left, r.bottom - r.top
    hdc = u.GetWindowDC(h); mdc = g.CreateCompatibleDC(hdc); bmp = g.CreateCompatibleBitmap(hdc, W, H); g.SelectObject(mdc, bmp)
    u.PrintWindow(h, mdc, 2)  # PW_RENDERFULLCONTENT captures WebView2 even if covered
    class BIH(ctypes.Structure):
        _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD), ("biBitCount", w.WORD),
                    ("biCompression", w.DWORD), ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                    ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]
    bi = BIH(); bi.biSize = ctypes.sizeof(BIH); bi.biWidth = W; bi.biHeight = -H; bi.biPlanes = 1; bi.biBitCount = 32
    buf = ctypes.create_string_buffer(W * H * 4); g.GetDIBits(mdc, bmp, 0, H, buf, ctypes.byref(bi), 0)
    Image.frombuffer("RGBA", (W, H), buf, "raw", "BGRA", 0, 1).convert("RGB").save(os.path.join(OUT, name), optimize=True)
    g.DeleteObject(bmp); g.DeleteDC(mdc); u.ReleaseDC(h, hdc); print("saved", name, W, H, flush=True)

api = ad.Api()
win = webview.create_window("ArchiveDrop", url=os.path.join(here, "ui", "index.html"), js_api=api, width=1120, height=860,
                            frameless=True, easy_drag=False, background_color="#0e0a1f")
api._window = win

def run():
    time.sleep(4)
    js = win.evaluate_js
    # 1) preview with chips
    js("document.getElementById('link').value='https://archive.org/details/alicesadventures19033gut'; check(true)")
    for _ in range(40):
        if js("document.querySelectorAll('.chip').length") : break
        time.sleep(0.5)
    js("document.querySelectorAll('.chip').forEach(c=>{if(!c.dataset.ext.includes('txt'))c.classList.remove('on')});"
       "document.getElementById('system').value='books'; updateWhere()")
    time.sleep(1); shot("preview.png")
    # 2) main: one finished job + a public-domain audiobook downloading 3 files at once
    js("go()"); time.sleep(6)
    from internetarchive import search_items
    ident = next(iter(search_items("collection:librivoxaudio AND title:(alice wonderland)", fields=["identifier"])))["identifier"]
    print("audiobook:", ident, flush=True)
    js(f"document.getElementById('link').value='https://archive.org/details/{ident}'; document.getElementById('system').value='audiobooks'; updateWhere();"
       "preview=null; bulk=null")
    api.add(f"https://archive.org/details/{ident}", "*.mp3", "audiobooks", True)
    js("document.getElementById('link').value=''; document.getElementById('system').value=''; updateWhere()")
    for _ in range(60):
        st = api.state()
        if len(st["lanes"]) >= 3 and all(l["have"] > 300000 for l in st["lanes"]): break
        time.sleep(0.5)
    time.sleep(1.2); shot("main.png")
    api.stop()
    # 3) phone settings with a dummy topic
    api._cfg.update(ntfy_enabled=True, ntfy_topic="archivedrop-demo4k7q2mxz", ntfy_since=str(int(time.time())))
    js("settings(true)"); time.sleep(3); shot("phone.png")
    while api.state()["busy"]: time.sleep(0.5)
    api._inbox.stop(); win.destroy()

threading.Thread(target=run, daemon=True).start()
webview.start()
shutil.rmtree(data, ignore_errors=True)
shutil.rmtree(DEMO, ignore_errors=True)
print("done", flush=True)
