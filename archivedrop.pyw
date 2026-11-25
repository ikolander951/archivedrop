"""ArchiveDrop - glass UI (pywebview / WebView2) over the IA AutoDL engine (ia_autodl.py)."""
import difflib
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", HERE))          # bundled ui/ and icon when installed
sys.path.insert(0, str(HERE))
import ia_autodl as core  # noqa: E402
import ntfy_inbox as phone  # noqa: E402

GUI_PID = core.HOME / "gui.pid"
FOLLOWUP_SECONDS = 15 * 60     # a bare folder name applies to links sent within this window


def _item_paths(dest, files):
    from internetarchive import utils as ia_utils
    out = []
    for f in files:
        rel = ia_utils.sanitize_windows_relpath(f.name)[0] if os.name == "nt" else f.name
        out.append(str(Path(dest) / rel))
    return out


def _gui_running():
    try:
        return core.pid_alive(int(GUI_PID.read_text().strip()))
    except Exception:
        return False


def _background():
    """Scheduled task: pick up phone links (if the window isn't open to do it live), then run the queue."""
    cfg = {**core.DEFAULT_CONFIG, **core.load_json(core.CONFIG_FILE, {})}
    core.apply_limits(cfg)
    added = []
    if cfg.get("ntfy_enabled") and cfg.get("ntfy_topic") and not _gui_running():
        try:
            systems = core.folder_choices(cfg)
            last = cfg.get("ntfy_last") or {}
            for m in phone.poll(cfg):
                cfg["ntfy_since"] = m.get("id")
                if phone.REPLY_TAG in (m.get("tags") or []):
                    continue
                links, types, system = phone.parse(m.get("message", ""), systems)
                if not links and (system or types) and last.get("lines") and time.time() - last.get("t", 0) < FOLLOWUP_SECONDS:
                    q, done_now, new = core.read_lines(core.QUEUE), [], []
                    for raw in last["lines"]:
                        spec = core.parse_line(raw)
                        line = " | ".join([raw.split("|")[0].strip(), types or ",".join(spec["globs"]),
                                           system or (spec["subfolder"] or "")]).rstrip(" |")
                        if raw in q:
                            q[q.index(raw)] = line
                            added = [line if x == raw else x for x in added]
                            new.append(line)
                        else:
                            done_now.append(spec["target"]); new.append(raw)
                    core.QUEUE.write_text("\n".join(q) + ("\n" if q else ""), encoding="utf-8")
                    last = {"t": time.time(), "lines": new}
                    phone.reply(cfg, "Updated." + (f" {', '.join(done_now)} already downloaded; open ArchiveDrop to move it."
                                                   if done_now else ""), "Updated")
                    continue
                lines_now = []
                for link in links:
                    line = " | ".join([link, types, system]).rstrip(" |")
                    if line not in core.read_lines(core.QUEUE):
                        core.append_line(core.QUEUE, line)
                    added.append(line)
                    lines_now.append(line)
                if links:
                    last = {"t": time.time(), "lines": lines_now}
                    when = "Downloading now." if core.in_window() else f"Downloads start at {core.LIMITS['start']}."
                    phone.reply(cfg, f"Added {len(links)} link(s) to {system or 'Downloads'}. {when}", "Added")
            core.save_json(core.CONFIG_FILE, {**core.load_json(core.CONFIG_FILE, {}), "ntfy_since": cfg.get("ntfy_since"),
                                              "ntfy_last": last})
        except Exception as e:
            core.log(f"PHONE: background check failed: {e}")
    core.HANDOVER = True                 # stop and let the window take over if it opens
    core.main()
    if added:
        done = "\n".join(core.read_lines(core.DONE))
        for line in added:
            name = core.parse_line(line)["target"]
            if line not in core.read_lines(core.QUEUE) and line.strip() in done:
                phone.reply(cfg, f"Done: {name}", "Done", "white_check_mark")


if "--background" in sys.argv:                       # scheduled task: no window
    _background()
    sys.exit(0)

import webview  # noqa: E402

APP = "ArchiveDrop"
VERSION = "1.9.4"
REPO = "ikolander951/archivedrop"
ASSET = "ArchiveDrop-Setup.exe"
UPDATE_EVERY = 6 * 3600


def _vtuple(v):
    import re as _re
    return tuple(int(x) for x in _re.findall(r"\d+", v or "")[:3]) or (0,)


def _current_version():
    # ARCHIVEDROP_FAKE_VERSION lets the updater be tested end to end against the latest release
    return os.environ.get("ARCHIVEDROP_FAKE_VERSION") or VERSION


class Api:
    def __init__(self):
        self._lock = threading.RLock()
        self._maxed = False
        self._window = None
        self._cfg = {**core.DEFAULT_CONFIG, **core.load_json(core.CONFIG_FILE, {})}
        core.apply_limits(self._cfg)
        for p in (core.QUEUE, core.WATCH):
            p.touch(exist_ok=True)
        self._jobs = {}          # id -> dict(raw, item, types, folder, status, kind, dest)
        self._by_raw = {}
        self._next_id = 1
        self._logs = []
        self._status = "Ready"
        self._lanes = {}         # one entry per file downloading right now
        self._active_ids = set()
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._worker = None
        core.LOG_HOOK = self._log
        self._phone_raws = set()
        self._started = set()        # queue lines the scheduler has begun
        self._job_files = {}         # line -> [absolute file paths] belonging to that item
        self._move_after = {}        # line -> folder to move into once it finishes
        self._inbox = phone.Listener(lambda: self._cfg, self._on_phone, self._set_since, self._log)
        try:
            GUI_PID.write_text(str(os.getpid()))
        except OSError:
            pass
        if self._cfg.get("ntfy_enabled") and self._cfg.get("ntfy_topic"):
            self._inbox.start()
        self._update = None          # dict(version, notes, url, size, page) when a newer release exists
        self._upd = {"state": "idle"}  # idle | checking | downloading | installing | error
        threading.Thread(target=self._update_loop, daemon=True).start()
        for raw in core.read_lines(core.QUEUE):
            try:
                if core.parse_line(raw):
                    self._new_job(raw, "Queued")
            except ValueError:
                pass
        if self._jobs:
            self._start_worker()

    # ---------- internals ----------
    def _log(self, line):
        with self._lock:
            self._logs.append(line)
            del self._logs[:-300]

    def _new_job(self, raw, status, kind="queued"):
        spec = core.parse_line(raw)
        with self._lock:
            jid = self._next_id
            self._next_id += 1
            self._jobs[jid] = dict(
                id=jid, raw=raw, status=status, kind=kind, dest=None,
                item=spec["target"] + (f"/{spec['file']}" if spec["file"] else ""),
                types=", ".join(g.lstrip("*") for g in spec["globs"]) or "All files",
                folder=spec["subfolder"] or "Downloads")
            self._by_raw[raw] = jid
            return jid

    def _set(self, raw, status, kind):
        with self._lock:
            jid = self._by_raw.get(raw)
            if jid in self._jobs:
                self._jobs[jid].update(status=status, kind=kind)
        self._phone_done(raw, kind, status)

    def _systems(self):
        return core.folder_choices(self._cfg)

    def _start_worker(self):
        if self._worker and self._worker.is_alive():
            self._wake.set()
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def _work(self):
        """Scheduler: keeps up to N files downloading at once, across every queued link."""
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
        cfg = dict(self._cfg)
        cfg["stop_event"] = self._stop_event
        n = max(1, min(6, int(cfg.get("parallel") or 3)))
        waited = False
        while not core.acquire_lock(cfg):
            if not waited:
                self._status = "Taking over from the background download"
                waited = True
            if self._stop_event.wait(2):
                self._status = "Stopped"
                return
        dest_root = Path(os.path.expandvars(cfg["download_dir"]))
        items = {}          # raw -> dict(left, errors, skipped, got)
        inflight = {}       # future -> (raw, file name)
        backlog = []        # (raw, ident, dest, (f, target, size)) not yet submitted
        done_lines = set()
        pool = ThreadPoolExecutor(max_workers=n)
        self._status = f"Downloading up to {n} files at a time"

        def next_line():
            for r in core.read_lines(core.QUEUE):
                if r.strip() and not r.strip().startswith("#") and r not in items and r not in done_lines:
                    return r
            return None

        def plan(raw):
            self._started.add(raw)      # from here on a follow-up folder name moves files instead of editing the line
            items[raw] = dict(left=0, errors=[], skipped=0, got=0, verified=0, planned=False)
            if raw not in self._by_raw:
                self._new_job(raw, "Queued")
            with self._lock:
                self._active_ids.add(self._by_raw[raw])
            self._set(raw, "Checking", "active")
            core.log(f"QUEUE: {raw.strip()}")
            try:
                spec = core.parse_line(raw)
                idents = [spec["target"]] if spec["kind"] == "item" else \
                    [r["identifier"] for r in __import__("internetarchive").search_items(spec["target"], fields=["identifier"])]
                for ident in idents:
                    dest, todo, skipped, allf = core.plan_item(ident, spec, cfg, dest_root)
                    self._job_files.setdefault(raw, []).extend(_item_paths(dest, allf))
                    with self._lock:
                        self._jobs[self._by_raw[raw]]["dest"] = str(dest)
                    items[raw]["skipped"] += skipped
                    items[raw]["left"] += len(todo)
                    backlog.extend((raw, ident, dest, job) for job in todo)
                items[raw]["planned"] = True
                self._set(raw, f"Downloading ({items[raw]['left']} file{'' if items[raw]['left'] == 1 else 's'})" if items[raw]["left"] else "Done", "active")
            except Exception as e:
                items[raw]["errors"].append(str(e))
                core.log(f"  !! {e}")
            maybe_finish(raw)

        def maybe_finish(raw):
            st = items[raw]
            if st["left"] > 0 or raw in done_lines:
                return
            done_lines.add(raw)
            with self._lock:
                self._active_ids.discard(self._by_raw.get(raw))
            if self._stop_event.is_set() and (st["left"] or not st.get("planned")):
                self._set(raw, "Paused", "paused")
                return
            state = core.load_json(core.STATE, {})
            attempts = state.setdefault("attempts", {})
            key = raw.strip()
            if not st["errors"]:
                core.finish_queue_line(raw, core.DONE, "ok")
                attempts.pop(key, None)
                if raw in self._move_after:
                    threading.Thread(target=self._move_job, args=(raw, self._move_after.pop(raw)), daemon=True).start()
                extra = f", {st['skipped']} already there" if st["skipped"] else ""
                ver = (", all verified" if st["verified"] == st["got"] else f", {st['verified']} verified") \
                    if st["verified"] else ""
                self._set(raw, f"Done ({st['got']} files{ver}{extra})", "done")
            elif self._stop_event.is_set():
                self._set(raw, "Paused", "paused")
            else:
                attempts[key] = attempts.get(key, 0) + 1
                msg = st["errors"][0] if len(st["errors"]) == 1 else f"{len(st['errors'])} files failed"
                if attempts[key] >= cfg["max_attempts"]:
                    core.finish_queue_line(raw, core.FAILED, msg)
                    attempts.pop(key, None)
                    self._set(raw, f"Failed: {msg}", "failed")
                else:
                    self._set(raw, f"Retrying later ({attempts[key]}/{cfg['max_attempts']}): {msg}", "failed")
            core.save_json(core.STATE, state)

        def run_file(raw, dest, job):
            f, target, size = job
            key = f"{raw}::{f.name}"
            lane = dict(name=f.name.rsplit("/", 1)[-1], target=str(target), size=size or 0,
                        started=time.time(), start_bytes=_size(target), phase="download", checked=0)
            with self._lock:
                self._lanes[key] = lane

            def on_phase(phase, n):
                lane.update(phase=phase, checked=n)
            try:
                return core.fetch_file(f, dest, target, size, cfg, on_phase)
            finally:
                with self._lock:
                    self._lanes.pop(key, None)

        try:
            while True:
                self._wake.clear()
                open_now = core.in_window()       # outside download hours nothing new starts
                # keep the pool fed; plan more links only when the backlog runs low
                while open_now and not self._stop_event.is_set() and len(inflight) < n:
                    if backlog:
                        raw, ident, dest, job = backlog.pop(0)
                        fut = pool.submit(run_file, raw, dest, job)
                        inflight[fut] = (raw, job[0].name)
                        self._status = f"Downloading up to {n} files at a time"
                        continue
                    raw = next_line()
                    if raw is None:
                        break
                    plan(raw)
                if not inflight:
                    if self._stop_event.is_set() or (not backlog and next_line() is None):
                        break
                    if not open_now:
                        self._status = core.hold_text()
                        self._stop_event.wait(5)
                    continue
                done, _ = wait(list(inflight), timeout=1, return_when=FIRST_COMPLETED)
                for fut in done:
                    raw, name = inflight.pop(fut)
                    st = items[raw]
                    st["left"] -= 1
                    try:
                        if fut.result() == "verified":
                            st["verified"] += 1
                        st["got"] += 1
                    except core.Stopped:
                        st["left"] += 1          # not downloaded; stays queued
                    except Exception as e:
                        st["errors"].append(f"{name}: {e}")
                        core.log(f"  !! {name}: {e}")
                    if st["left"] > 0:
                        self._set(raw, f"Downloading ({st['left']} file{'' if st['left'] == 1 else 's'} left)", "active")
                    maybe_finish(raw)
                if self._stop_event.is_set():
                    backlog.clear()          # untouched files stay queued for next time
                    if not inflight:
                        for raw, st in items.items():
                            if raw not in done_lines:
                                done_lines.add(raw)
                                self._set(raw, "Paused", "paused")
                                with self._lock:
                                    self._active_ids.discard(self._by_raw.get(raw))
                        break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            core.LOCK.unlink(missing_ok=True)
            with self._lock:
                self._lanes.clear()
                self._active_ids.clear()
            self._status = "Stopped" if self._stop_event.is_set() else "All done"
            # a link added in the last instant before this run ended still gets picked up
            if not self._stop_event.is_set() and any(
                    r.strip() and not r.strip().startswith("#") and r not in items
                    for r in core.read_lines(core.QUEUE)):
                threading.Timer(0.5, self._start_worker).start()

    # ---------- phone (ntfy) ----------
    def _save_cfg(self):
        core.save_json(core.CONFIG_FILE, {k: v for k, v in self._cfg.items() if k in core.DEFAULT_CONFIG})

    def _set_since(self, mid):
        if mid:
            self._cfg["ntfy_since"] = mid
            self._save_cfg()

    def _on_phone(self, msg):
        text = msg.get("message", "")
        links, types, system = phone.parse(text, self._systems())
        if not links:
            recent = self._cfg.get("ntfy_last") or {}
            if (system or types) and recent.get("lines") and time.time() - recent.get("t", 0) < FOLLOWUP_SECONDS:
                self._retarget(recent["lines"], system, types)
                return
            hint = (f"'{text.strip()[:30]}' isn't a folder here." if text.strip() and not (system or types)
                    else "There was no recent link to apply that to.") if text.strip() else ""
            phone.reply(self._cfg, ("No archive.org link in that message. " + hint).strip() +
                        " Send a link, or a folder name right after one.", "Nothing added", "warning")
            return
        core.log(f"PHONE: {len(links)} link(s) -> {system or 'Downloads'}")
        lines = []
        for link in links:
            r = self.add(link, types, system, True)
            if r.get("ok"):
                self._phone_raws.add(r["line"])
                lines.append(r["line"])
        self._cfg["ntfy_last"] = {"t": time.time(), "lines": lines}
        self._save_cfg()
        names = ", ".join(core.parse_line(l.split('|')[0])["target"] for l in links[:3]) + (" ..." if len(links) > 3 else "")
        phone.reply(self._cfg, f"{names} -> {system or 'Downloads'}" + (f" ({types.replace('*', '')})" if types else ""),
                    f"Added {len(links)} to ArchiveDrop")

    def _retarget(self, lines, system, types):
        """Apply a follow-up 'books' / '.pdf' message to the links sent just before it."""
        changed, moving, late = [], [], []
        new_lines = []
        for raw in lines:
            spec = core.parse_line(raw)
            name = spec["target"]
            link = raw.split("|")[0].strip()
            new_types = types or ",".join(spec["globs"])
            new_sys = system or (spec["subfolder"] or "")
            line = " | ".join([link, new_types, new_sys]).rstrip(" |")
            jid = self._by_raw.get(raw)
            queued = raw in core.read_lines(core.QUEUE) and raw not in self._started
            if line == raw:
                new_lines.append(raw)
                continue
            if queued:
                q = core.read_lines(core.QUEUE)
                q[q.index(raw)] = line
                core.QUEUE.write_text("\n".join(q) + "\n", encoding="utf-8")
                with self._lock:
                    self._by_raw.pop(raw, None)
                    if jid in self._jobs:
                        del self._jobs[jid]
                self._phone_raws.discard(raw)
                self._new_job(line, "Queued")
                self._phone_raws.add(line)
                new_lines.append(line)
                changed.append(name)
            elif system:
                new_lines.append(raw)
                if jid in self._jobs and self._jobs[jid]["kind"] == "done":
                    threading.Thread(target=self._move_job, args=(raw, system), daemon=True).start()
                else:
                    self._move_after[raw] = system
                moving.append(name)
            else:
                new_lines.append(raw)
                late.append(name)
        self._cfg["ntfy_last"] = {"t": time.time(), "lines": new_lines}
        self._save_cfg()
        bits = []
        if changed:
            bits.append(f"{', '.join(changed)} will go to {system or 'Downloads'}" + (f" ({types.replace('*', '')})" if types else ""))
        if moving:
            bits.append(f"{', '.join(moving)} already started, so it moves to {system} when it finishes")
        if late:
            bits.append(f"too late to change file types for {', '.join(late)}")
        phone.reply(self._cfg, ". ".join(bits) + "." if bits else "Nothing to change.", "Updated")

    def _move_job(self, raw, system):
        """Move one finished item's files into a folder (only that item's files)."""
        root = Path(os.path.expandvars(self._cfg["download_dir"]))
        dst_root = Path(system) if Path(system).is_absolute() else root / core.safe_rel(system)
        files = [Path(f) for f in self._job_files.get(raw, []) if Path(f).is_file()]
        name = core.parse_line(raw)["target"]
        if not files:
            phone.reply(self._cfg, f"Couldn't find the files for {name} to move.", "Move skipped", "warning")
            return
        base = Path(os.path.commonpath([str(f.parent) for f in files]))
        moved = 0
        for f in files:
            rel = f.relative_to(base)
            target = dst_root / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.stat().st_size == f.stat().st_size:
                    f.unlink()
                else:
                    os.replace(f, target)
                moved += 1
            except OSError as e:
                core.log(f"  !! couldn't move {f.name}: {e}")
        for d in sorted({p.parent for p in files}, key=lambda p: len(p.parts), reverse=True):
            try:
                while d != d.parent and d != root and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
            except OSError:
                pass
        core.log(f"MOVE: {name}: {moved} files -> {dst_root}")
        with self._lock:
            jid = self._by_raw.get(raw)
            if jid in self._jobs:
                self._jobs[jid].update(dest=str(dst_root), folder=system,
                                       status=f"Done, moved to {system}")
        phone.reply(self._cfg, f"Moved {moved} files from {name} to {system}.", "Moved", "file_folder")

    def _phone_done(self, raw, kind, status):
        if raw in self._phone_raws and kind in ("done", "failed") and not status.startswith("Retrying"):
            self._phone_raws.discard(raw)
            name = core.parse_line(raw)["target"]
            ok = kind == "done"
            threading.Thread(target=phone.reply, args=(self._cfg, f"{name}: {status}",
                             "Downloaded" if ok else "Download failed",
                             "white_check_mark" if ok else "x"), daemon=True).start()

    # ---------- API for the page ----------
    def state(self, log_from=0):
        with self._lock:
            lanes, total = [], 0.0
            now = time.time()
            for ln in self._lanes.values():
                if ln.get("phase") == "verifying":
                    lanes.append(dict(name=ln["name"], have=ln["checked"], size=ln["size"], speed=0, eta=0,
                                      phase="verifying"))
                    continue
                have = _size(ln["target"])
                el = max(now - ln["started"], 0.001)
                speed = max(have - ln["start_bytes"], 0) / el
                total += speed
                lanes.append(dict(name=ln["name"], have=have, size=ln["size"], speed=speed, phase="download",
                                  eta=(ln["size"] - have) / speed if speed > 0 and ln["size"] else 0))
            return dict(
                hold=core.hold_text(), limit=core.LIMITS["kbps"] * 1024,
                jobs=[{k: v for k, v in j.items() if k != "raw"} for j in self._jobs.values()],
                busy=bool(self._worker and self._worker.is_alive()),
                status=self._status, lanes=lanes, phone=self._inbox.status, update=self._update, upd=dict(self._upd), speed=total, parallel=int(self._cfg.get("parallel") or 3),
                log=self._logs[log_from:] if log_from <= len(self._logs) else self._logs[-80:],
                log_len=len(self._logs))

    def info(self):
        return dict(systems=self._systems(), root=self._cfg["download_dir"],
                    unsorted=self._cfg.get("unsorted_dir") or self._cfg["download_dir"])

    def check(self, link):
        link = (link or "").strip().strip('"')
        try:
            spec = core.parse_line(link.split("|")[0])
        except ValueError as e:
            return dict(ok=False, error=str(e))
        if not spec:
            return dict(ok=False, error="Paste an archive.org link or item ID.")
        if spec["kind"] != "item":
            return dict(ok=False, error="Searches can't be previewed. They download every matching item.")
        try:
            from internetarchive import get_item
            item = get_item(spec["target"], request_kwargs={"timeout": 60})
            if not item.exists:
                return dict(ok=False, error="No item with that ID. It may have been removed or misspelled.")
            files = [f for f in item.get_files() if core.wanted(f, {**spec, "globs": []}, self._cfg)]
        except Exception as e:
            return dict(ok=False, error=f"Couldn't reach archive.org: {e}")
        counts, sizes = Counter(), Counter()
        for f in files:
            ext = Path(f.name).suffix.lower() or "no extension"
            counts[ext] += 1
            sizes[ext] += int(f.size or 0)
        return dict(ok=True, key=f"{spec['target']}/{spec['file'] or ''}",
                    title=item.metadata.get("title") or spec["target"], single=bool(spec["file"]),
                    count=len(files), size=sum(sizes.values()),
                    exts=[dict(ext=e, n=n, size=sizes[e]) for e, n in counts.most_common(14)])

    def add(self, link, types, system, force=False):
        link = (link or "").strip().strip('"')
        system = (system or "").strip().strip('"')
        types = (types or "").strip()
        if not link:
            return dict(ok=False, error="Paste a link first.")
        systems = self._systems()
        if system and not force and not Path(system).is_absolute() and systems and system not in systems:
            close = difflib.get_close_matches(system.lower(), systems, n=3, cutoff=0.5)
            return dict(ok=False, confirm=True, system=system, suggestions=close)
        line = link if "|" in link else " | ".join([link, types, system]).rstrip(" |")
        try:
            core.parse_line(line)
        except ValueError as e:
            return dict(ok=False, error=str(e))
        if line not in core.read_lines(core.QUEUE):      # same link + options twice = one job
            core.append_line(core.QUEUE, line)
        if line not in self._by_raw:
            self._new_job(line, "Queued")
        self._start_worker()
        return dict(ok=True, line=line)

    def add_many(self, links, types, system, force=False):
        links = [l.strip().strip('"') for l in (links or []) if l and l.strip()]
        if not links:
            return dict(ok=False, error="No links found.")
        bad = []
        for i, link in enumerate(links):
            r = self.add(link, types, system, force or i > 0)
            if r.get("confirm"):
                return r
            if not r.get("ok"):
                bad.append(link)
        return dict(ok=not bad or len(bad) < len(links), added=len(links) - len(bad), bad=bad)

    # ---------- updates (GitHub releases) ----------
    def _update_loop(self):
        time.sleep(5)
        while True:
            if self._cfg.get("auto_update_check", True):
                self._check_update()
            time.sleep(UPDATE_EVERY)

    def _check_update(self):
        import requests
        try:
            r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=20,
                             headers={"Accept": "application/vnd.github+json", "User-Agent": f"ArchiveDrop/{VERSION}"})
            r.raise_for_status()
            rel = r.json()
            asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET), None)
            newer = _vtuple(rel.get("tag_name")) > _vtuple(_current_version())
            self._update = dict(version=rel.get("tag_name", "").lstrip("v"), notes=rel.get("body") or "",
                                page=rel.get("html_url"), url=asset and asset["browser_download_url"],
                                size=asset and asset["size"]) if newer and asset else None
            if self._update:
                core.log(f"UPDATE: {self._update['version']} is available")
            return dict(ok=True, update=self._update)
        except Exception as e:
            return dict(ok=False, error=f"Couldn't check for updates: {e.__class__.__name__}")

    def update_check(self):
        r = self._check_update()
        r["current"] = VERSION
        return r

    def update_install(self):
        """Download the new installer (verified against the size GitHub reports), run it silently, then quit.
        The installer upgrades in place, keeps settings and tasks, and reopens ArchiveDrop."""
        u = self._update
        if not u:
            return dict(ok=False, error="No update available.")
        if not getattr(sys, "frozen", False):
            return dict(ok=False, error="Running from source: pull the new version from GitHub instead.", page=u["page"])
        if core.PORTABLE:
            import webbrowser
            webbrowser.open(u["page"])
            return dict(ok=False, error="Portable copy: opened the release page. Download the new zip and unzip it over this folder.",
                        page=u["page"])
        if self._upd["state"] in ("downloading", "installing"):
            return dict(ok=True)
        if not u["url"].startswith(f"https://github.com/{REPO}/releases/download/"):
            return dict(ok=False, error="Unexpected download address; update cancelled.")
        threading.Thread(target=self._do_update, args=(u,), daemon=True).start()
        return dict(ok=True)

    def _do_update(self, u):
        import requests, tempfile, subprocess
        dest = Path(tempfile.gettempdir()) / f"ArchiveDrop-Setup-{u['version']}.exe"
        self._upd = {"state": "downloading", "have": 0, "size": u["size"]}
        try:
            with requests.get(u["url"], stream=True, timeout=(15, 120),
                              headers={"User-Agent": f"ArchiveDrop/{VERSION}"}) as r:
                r.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in r.iter_content(1 << 16):
                        fh.write(chunk)
                        self._upd["have"] += len(chunk)
            if dest.stat().st_size != u["size"]:
                raise IOError("download size didn't match the release")
            self._upd = {"state": "installing"}
            core.log(f"UPDATE: installing {u['version']}")
            self._stop_event.set()                     # running downloads pause here and resume after
            self._inbox.stop()
            try:
                GUI_PID.unlink()
            except OSError:
                pass
            env = {k: v for k, v in os.environ.items() if k != "ARCHIVEDROP_FAKE_VERSION"}
            subprocess.Popen([str(dest), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                             close_fds=True, env=env, creationflags=0x00000008 | 0x00000200)   # detached, own group
            time.sleep(1.5)
            os._exit(0)                                # installer replaces the app files, then reopens it
        except Exception as e:
            self._upd = {"state": "error", "error": str(e)}
            core.log(f"UPDATE: failed: {e}")

    def phone_new_topic(self):
        return phone.new_topic()

    def phone_qr(self, topic, server):
        host = (server or "https://ntfy.sh").split("://", 1)[-1].rstrip("/")
        url = f"ntfy://{host}/{topic}"
        return dict(svg=phone.qr_svg(url), url=url)

    def phone_test(self, topic, server, token):
        cfg = {"ntfy_enabled": True, "ntfy_topic": topic.strip(), "ntfy_server": server or "https://ntfy.sh",
               "ntfy_token": token}
        if not cfg["ntfy_topic"]:
            return dict(ok=False, error="Pick a topic first.")
        try:
            import requests
            r = requests.post(phone._base(cfg), timeout=15, data="Your phone is connected to ArchiveDrop.".encode(),
                              headers={**phone._headers(cfg), "Title": "ArchiveDrop test", "Tags": "archivedrop,tada"})
            r.raise_for_status()
            return dict(ok=True)
        except Exception as e:
            return dict(ok=False, error=f"Couldn't reach ntfy: {e}")

    def stop(self):
        self._stop_event.set()
        self._status = "Stopping (partly downloaded files pick up where they left off next time)"
        return True

    def remove(self, jid):
        with self._lock:
            j = self._jobs.get(jid)
            if not j:
                return dict(ok=True)
            if j["kind"] == "active":
                return dict(ok=False, error="That one is downloading. Stop it first.")
            lines = core.read_lines(core.QUEUE)
            if j["raw"] in lines:
                lines.remove(j["raw"])
                core.QUEUE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            self._by_raw.pop(j["raw"], None)
            del self._jobs[jid]
        return dict(ok=True)

    def clear_finished(self):
        with self._lock:
            for jid in [k for k, j in self._jobs.items() if j["kind"] in ("done", "failed") and
                        not j["status"].startswith("Retrying")]:
                self._by_raw.pop(self._jobs[jid]["raw"], None)
                del self._jobs[jid]
        return True

    def open_folder(self, jid=None):
        j = self._jobs.get(jid) if jid else None
        root = Path(os.path.expandvars(self._cfg["download_dir"]))
        if j and j.get("dest"):
            p = Path(j["dest"])
        elif j:
            spec = core.parse_line(j["raw"])
            if spec["subfolder"]:
                s = Path(spec["subfolder"])
                p = s if s.is_absolute() else root / spec["subfolder"]
            else:
                p = Path(os.path.expandvars(self._cfg.get("unsorted_dir") or str(root))) / spec["target"]
        else:
            p = root
        _open(p)
        return True

    def open_logs(self):
        _open(core.LOGDIR)
        return True

    # ---- archive.org account (the password goes straight to archive.org and is never stored) ----
    def account_sign_in(self, email, password):
        try:
            name = core.sign_in(email, password)
        except ValueError as e:
            return dict(ok=False, error=str(e))
        core.log(f"ACCOUNT: signed in as {name}")
        return dict(ok=True, account=name)

    def account_sign_in_keys(self, access, secret):
        try:
            name = core.sign_in_keys(access, secret)
        except ValueError as e:
            return dict(ok=False, error=str(e))
        core.log(f"ACCOUNT: signed in as {name} (access keys)")
        return dict(ok=True, account=name)

    def account_keys_page(self):
        import webbrowser
        webbrowser.open(core.KEYS_PAGE)
        return True

    def account_sign_out(self):
        core.sign_out()
        core.log("ACCOUNT: signed out")
        return dict(ok=True)

    def settings_get(self):
        return dict(root=self._cfg.get("download_dir") or "", unsorted=self._cfg.get("unsorted_dir") or "",
                    originals=bool(self._cfg.get("originals_only", True)),
                    parallel=int(self._cfg.get("parallel") or 3), version=VERSION,
                    ntfy_enabled=bool(self._cfg.get("ntfy_enabled")), ntfy_topic=self._cfg.get("ntfy_topic") or "",
                    ntfy_server=self._cfg.get("ntfy_server") or "https://ntfy.sh",
                    ntfy_token=self._cfg.get("ntfy_token") or "",
                    auto_update=bool(self._cfg.get("auto_update_check", True)),
                    account=core.account(), portable=core.PORTABLE,
                    verify=bool(self._cfg.get("verify_checksums", True)),
                    limit_mb=round((self._cfg.get("speed_limit_kbps") or 0) / 1024, 1),
                    sched=bool(self._cfg.get("schedule_enabled")),
                    sched_start=core.valid_hhmm(self._cfg.get("schedule_start"), "01:00"),
                    sched_end=core.valid_hhmm(self._cfg.get("schedule_end"), "07:00"))

    def settings_save(self, root, unsorted, originals, parallel=3, ph=None, auto_update=None, lim=None):
        if lim is not None:
            self._cfg["verify_checksums"] = bool(lim.get("verify", True))
            try:
                mb = max(0.0, float(lim.get("limit_mb") or 0))
            except (TypeError, ValueError):
                mb = 0.0
            self._cfg["speed_limit_kbps"] = int(round(mb * 1024))
            self._cfg["schedule_enabled"] = bool(lim.get("sched"))
            self._cfg["schedule_start"] = core.valid_hhmm(lim.get("start"), "01:00")
            self._cfg["schedule_end"] = core.valid_hhmm(lim.get("end"), "07:00")
            core.apply_limits(self._cfg)
            self._wake.set()
        self._cfg["download_dir"] = (root or "").strip() or core.DEFAULT_CONFIG["download_dir"]
        self._cfg["unsorted_dir"] = (unsorted or "").strip() or None
        self._cfg["originals_only"] = self._cfg["skip_ia_metadata"] = bool(originals)
        self._cfg["parallel"] = max(1, min(6, int(parallel or 3)))
        if auto_update is not None:
            self._cfg["auto_update_check"] = bool(auto_update)
        if ph is not None:
            before = (self._cfg.get("ntfy_enabled"), self._cfg.get("ntfy_topic"), self._cfg.get("ntfy_server"),
                      self._cfg.get("ntfy_token"))
            self._cfg["ntfy_enabled"] = bool(ph.get("enabled"))
            self._cfg["ntfy_topic"] = (ph.get("topic") or "").strip()
            self._cfg["ntfy_server"] = (ph.get("server") or "").strip() or "https://ntfy.sh"
            self._cfg["ntfy_token"] = (ph.get("token") or "").strip()
            after = (self._cfg["ntfy_enabled"], self._cfg["ntfy_topic"], self._cfg["ntfy_server"], self._cfg["ntfy_token"])
            if after != before:
                if after[1] != before[1] or after[2] != before[2]:
                    self._cfg["ntfy_since"] = None
                self._inbox.stop()
                if self._cfg["ntfy_enabled"] and self._cfg["ntfy_topic"]:
                    if not self._cfg.get("ntfy_since"):
                        try:
                            self._cfg["ntfy_since"] = phone.latest_id(self._cfg) or None
                        except Exception:
                            pass
                        # fresh topic: start from "now" (ntfy accepts a unix time) so the background check has a point to read from
                        self._cfg["ntfy_since"] = self._cfg.get("ntfy_since") or str(int(time.time()))
                    self._inbox.start()
        core.save_json(core.CONFIG_FILE, {k: v for k, v in self._cfg.items() if k in core.DEFAULT_CONFIG})
        return self.info()

    def pick_folder(self, start=""):
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=start or str(Path.home()))
        return res[0] if res else ""

    # window chrome (frameless)
    def minimize(self):
        self._window.minimize()

    def toggle_max(self):
        h = _FRAME.get("hwnd")
        if h:                                   # ask Windows: Snap and double-clicks change it too
            import ctypes
            self._maxed = bool(ctypes.windll.user32.IsZoomed(ctypes.c_void_p(h)))
        if self._maxed:
            self._window.restore()
        else:
            self._window.maximize()
        self._maxed = not self._maxed

    def close(self, force=False):
        if not force and self._worker and self._worker.is_alive():
            return dict(confirm=True)
        self._stop_event.set()                  # downloads stop mid-file; gui.pid stays until the process exits
        self._inbox.stop()
        threading.Timer(0.22, self._window.destroy).start()   # after the page's fade-out
        return dict(ok=True)


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _open(p):
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(p))
    else:
        subprocess.Popen(["xdg-open", str(p)])


def _focus_existing():
    """If ArchiveDrop's window is already open, bring it forward and return True (single instance)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        pid = int(GUI_PID.read_text().strip())
        if pid == os.getpid() or not core.pid_alive(pid):
            return False
        k32, u = ctypes.windll.kernel32, ctypes.windll.user32
        h = k32.OpenProcess(0x1000, False, pid)                      # make sure that pid is really ArchiveDrop
        buf = ctypes.create_unicode_buffer(520); n = ctypes.c_ulong(520)
        ok = h and k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        if h:
            k32.CloseHandle(h)
        if not ok or Path(buf.value).name.lower() != Path(sys.executable).name.lower():
            return False
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def each(hw, _):
            owner = ctypes.c_ulong()
            u.GetWindowThreadProcessId(ctypes.c_void_p(hw), ctypes.byref(owner))
            t = ctypes.create_unicode_buffer(64)
            u.GetWindowTextW(ctypes.c_void_p(hw), t, 64)
            if owner.value == pid and t.value == APP:
                found.append(hw)
            return True

        u.EnumWindows(each, None)
        if not found:
            return False
        hw = ctypes.c_void_p(found[0])
        u.ShowWindow(hw, 9)                                           # SW_RESTORE
        u.SetForegroundWindow(hw)
        return True
    except Exception:
        return False


def main():
    if _focus_existing():
        return
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JB.ArchiveDrop")
        except Exception:
            pass
    api = Api()
    api._window = webview.create_window(
        APP, url=str(RES / "ui" / "index.html"), js_api=api, width=1080, height=820,
        min_size=(880, 660), frameless=True, easy_drag=False, background_color="#0e0a1f")
    api._window.events.shown += lambda: threading.Timer(0.3, _set_taskbar_icon).start()
    api._window.events.shown += lambda: threading.Timer(0.15, _native_frame, args=(api._window,)).start()
    webview.start(icon=str(RES / "archivedrop.ico"))
    # Window closed: let running downloads wind down, then really exit. Leftover threads used to keep a
    # windowless copy downloading, and a reopened window would then download the same files alongside it.
    api._stop_event.set()
    w = api._worker
    if w is not None and w.is_alive():
        w.join(20)
    try:
        if core.LOCK.read_text().strip() == str(os.getpid()):
            core.LOCK.unlink()
    except OSError:
        pass
    try:
        if GUI_PID.read_text().strip() == str(os.getpid()):
            GUI_PID.unlink()
    except OSError:
        pass
    os._exit(0)


_FRAME = {}


def _app_hwnd():
    import ctypes
    u = ctypes.windll.user32
    found, pid = [], os.getpid()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def each(h, _):
        owner = ctypes.c_ulong()
        u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(owner))
        buf = ctypes.create_unicode_buffer(64)
        u.GetWindowTextW(ctypes.c_void_p(h), buf, 64)
        if owner.value == pid and buf.value == APP:
            found.append(h)
        return True

    u.EnumWindows(each, None)
    return found[0] if found else None


def _native_frame(window=None):
    """Give the frameless window a real frame that takes up no space. With caption and sizing styles
    Windows animates minimize, maximize, restore and close, offers Snap, and draws the shadow; the
    WM_NCCALCSIZE handler keeps the whole window as page so nothing of that frame is visible."""
    if os.name != "nt" or "hwnd" in _FRAME:
        return
    _FRAME["window"] = window
    try:
        import ctypes
        from ctypes import wintypes as w
        u, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        hwnd = _app_hwnd()
        if not hwnd:
            return
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM)
        u.CallWindowProcW.restype = LRESULT
        u.CallWindowProcW.argtypes = [ctypes.c_void_p, w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM]
        u.SetWindowLongPtrW.restype = ctypes.c_void_p
        u.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_void_p]
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
        u.SetWindowLongW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_long]
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.IsZoomed.argtypes = [w.HWND]
        u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]

        class NCCALCSIZE_PARAMS(ctypes.Structure):
            _fields_ = [("rgrc", w.RECT * 3), ("lppos", ctypes.c_void_p)]

        class MARGINS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]

        RIM = 6

        def rim(on):
            form = getattr(_FRAME.get("window"), "native", None)
            if form is None or _FRAME.get("rim") == on:
                return
            _FRAME["rim"] = on
            try:
                from System import Action
                from System.Windows.Forms import Padding
                form.BeginInvoke(Action(lambda: setattr(form, "Padding", Padding(RIM if on else 0))))
            except Exception:
                pass

        def proc(h, msg, wp, lp):
            if msg == 0x0084 and not u.IsZoomed(h):    # WM_NCHITTEST on the rim: let Windows resize from it
                x, y = ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short((lp >> 16) & 0xFFFF).value
                r = w.RECT()
                u.GetWindowRect(h, ctypes.byref(r))
                edge = 12                               # corners are easier to grab than the sides
                left, right = x < r.left + RIM, x >= r.right - RIM
                top, bottom = y < r.top + RIM, y >= r.bottom - RIM
                cl, cr = x < r.left + edge, x >= r.right - edge
                ct, cb = y < r.top + edge, y >= r.bottom - edge
                if (top and cl) or (left and ct): return 13
                if (top and cr) or (right and ct): return 14
                if (bottom and cl) or (left and cb): return 16
                if (bottom and cr) or (right and cb): return 17
                if left: return 10
                if right: return 11
                if top: return 12
                if bottom: return 15
            if msg == 0x0005:                           # WM_SIZE: no rim while maximized
                rim(wp != 2)
            if msg == 0x0083 and wp:                    # WM_NCCALCSIZE: no visible frame
                if u.IsZoomed(h):                       # maximized: fill the work area, not past the screen edge
                    mi = MONITORINFO()
                    mi.cbSize = ctypes.sizeof(MONITORINFO)
                    if u.GetMonitorInfoW(u.MonitorFromWindow(h, 2), ctypes.byref(mi)):
                        ctypes.cast(lp, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents.rgrc[0] = mi.rcWork
                return 0
            return u.CallWindowProcW(_FRAME["old"], h, msg, wp, lp)

        _FRAME["proc"] = WNDPROC(proc)                  # keep a reference: Windows calls it for the window's lifetime
        _FRAME["old"] = u.SetWindowLongPtrW(hwnd, -4, ctypes.cast(_FRAME["proc"], ctypes.c_void_p))
        style = u.GetWindowLongW(hwnd, -16)
        # WS_CAPTION | WS_SYSMENU | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        u.SetWindowLongW(hwnd, -16, style | 0x00C00000 | 0x00080000 | 0x00040000 | 0x00020000 | 0x00010000)
        dwm.DwmExtendFrameIntoClientArea(ctypes.c_void_p(hwnd), ctypes.byref(MARGINS(0, 0, 0, 1)))   # shadow
        pref = ctypes.c_int(2)                                                                     # rounded corners (Win 11)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), 4)
        u.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)  # SWP_FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER
        _FRAME["hwnd"] = hwnd
        rim(not u.IsZoomed(hwnd))
    except Exception as e:
        _FRAME.clear()
        core.log(f"WINDOW: native frame unavailable ({e.__class__.__name__}: {e})")


def _set_taskbar_icon():
    """WebView2 windows inherit pythonw's icon; swap in ours for the title/taskbar/Alt-Tab."""
    if os.name != "nt":
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        u.LoadImageW.restype = ctypes.c_void_p
        u.FindWindowW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        found = []
        pid = os.getpid()

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def each(h, _):
            owner = ctypes.c_ulong()
            u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(owner))
            buf = ctypes.create_unicode_buffer(64)
            u.GetWindowTextW(ctypes.c_void_p(h), buf, 64)
            if owner.value == pid and buf.value == APP:
                found.append(h)
            return True

        u.EnumWindows(each, None)
        hwnd = found[0] if found else None
        ico = str(RES / "archivedrop.ico")
        for kind, size in ((0, 16), (1, 32)):          # ICON_SMALL, ICON_BIG
            h = u.LoadImageW(None, ico, 1, size * 2 if kind else size, size * 2 if kind else size, 0x10)
            if hwnd and h:
                u.SendMessageW(hwnd, 0x80, kind, h)     # WM_SETICON
    except Exception:
        pass


if __name__ == "__main__":
    main()
