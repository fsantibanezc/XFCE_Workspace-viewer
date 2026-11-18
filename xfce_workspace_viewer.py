#!/usr/bin/env python3
"""XFCE Workspace Viewer: a live, view-only window onto another XFCE workspace.

xfwm4 does not draw windows on workspaces you are not looking at (it unmaps them). To watch them live,
this tool temporarily "parks" the target workspace's windows: makes them sticky and hidden from the
taskbar/pager, and moves them under the bottom-right corner of the panel, where they stay mapped and the
compositor keeps rendering them. Their pixels are captured with MIT-SHM and drawn at their original
positions. Switching to the target workspace, choosing another one, or closing the viewer puts every
window back exactly where it was. If the viewer ever dies, run:  python xfce_workspace_viewer.py --restore

    xfce-workspace-viewer [--workspace N] [--fps 5]
    xfce-workspace-viewer --restore
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from PyQt5.QtCore import QObject, QRect, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QImage, QPainter
from PyQt5.QtWidgets import (QApplication, QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                             QSizePolicy, QSpinBox,
                             QVBoxLayout, QWidget)

from x11util import DamageTracker, X11, ShmCapture

APP_NAME = "XFCE Workspace Viewer"
STATE_FILE = Path.home() / ".cache" / "xfce-workspace-viewer" / "parked.json"
LEGACY_STATE_FILE = Path.home() / ".cache" / "desktopindesktop" / "parked.json"  # pre-rename versions
STICKY = 0xFFFFFFFF
SKIP_TYPES = {"_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DESKTOP", "_NET_WM_WINDOW_TYPE_NOTIFICATION",
              "_NET_WM_WINDOW_TYPE_TOOLTIP", "_NET_WM_WINDOW_TYPE_POPUP_MENU",
              "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU", "_NET_WM_WINDOW_TYPE_SPLASH"}
LIST_EVERY = 0.5   # seconds between window-list refreshes
BG_EVERY = 3.0     # seconds between wallpaper refreshes


# =================================================================================================
# Parking: reversible window moves, persisted so a crash can be undone with --restore
# =================================================================================================
def _wmctrl(*args):
    subprocess.run(["wmctrl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Parker:
    def __init__(self, x: X11, display: str):
        self.x = x
        self.display = display
        self.records = self._load()

    # --- persistence ----------------------------------------------------------
    def _load(self) -> dict:
        try:
            return {int(k, 16): v for k, v in json.loads(STATE_FILE.read_text()).items()}
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({hex(k): v for k, v in self.records.items()}, indent=1))
        os.replace(tmp, STATE_FILE)

    # --- operations -----------------------------------------------------------
    def is_parked(self, wid: int) -> bool:
        return wid in self.records

    def park(self, w, order: int):
        if w.wid in self.records:
            return
        st = w.states
        rec = {
            "title": w.title, "desktop": w.desktop, "order": order,
            "cx": w.cx, "cy": w.cy, "cw": w.cw, "ch": w.ch,
            "fx": w.fx, "fy": w.fy, "fw": w.fw, "fh": w.fh,
            "maximized": [s for s in ("maximized_vert", "maximized_horz")
                          if f"_NET_WM_STATE_{s.upper()}" in st],
            "fullscreen": "_NET_WM_STATE_FULLSCREEN" in st,
            "added": [s for s in ("skip_taskbar", "skip_pager") if f"_NET_WM_STATE_{s.upper()}" not in st],
        }
        self.records[w.wid] = rec  # persist BEFORE touching the window
        self._save()

        wid = hex(w.wid)
        if rec["fullscreen"]:
            _wmctrl("-i", "-r", wid, "-b", "remove,fullscreen")
        if rec["maximized"]:
            _wmctrl("-i", "-r", wid, "-b", "remove," + ",".join(rec["maximized"]))
        # Move while still unmapped (no flash), keeping its size; xfwm4 clamps it to the bottom-right corner,
        # leaving a few pixels under the panel.
        sw, sh = self.x.screen_size()
        _wmctrl("-i", "-r", wid, "-e", f"10,{sw * 3},{sh * 3},{w.cw},{w.ch}")
        if rec["added"]:
            _wmctrl("-i", "-r", wid, "-b", "add," + ",".join(rec["added"]))
        _wmctrl("-i", "-r", wid, "-b", "add,sticky")  # sticky last: this is what makes it mapped

    def restore(self, wid: int):
        rec = self.records.get(wid)
        if rec is None:
            return
        h = hex(wid)
        if self.x.attributes(wid) is not None:  # window still exists
            _wmctrl("-i", "-r", h, "-b", "remove,sticky")
            _wmctrl("-i", "-r", h, "-t", str(rec["desktop"]))
            if rec["added"]:
                _wmctrl("-i", "-r", h, "-b", "remove," + ",".join(rec["added"]))
            _wmctrl("-i", "-r", h, "-e", f"10,{rec['cx']},{rec['cy']},{rec['cw']},{rec['ch']}")
            if rec["maximized"]:
                _wmctrl("-i", "-r", h, "-b", "add," + ",".join(rec["maximized"]))
            if rec["fullscreen"]:
                _wmctrl("-i", "-r", h, "-b", "add,fullscreen")
        del self.records[wid]
        self._save()

    def restore_all(self):
        # Bottom-most first so the original stacking order is approximately rebuilt.
        for wid in sorted(self.records, key=lambda k: self.records[k].get("order", 0)):
            self.restore(wid)
        self.x.sync()


def restore_leftovers(display: str) -> int:
    if LEGACY_STATE_FILE.exists():  # carry over windows parked by a pre-rename version
        try:
            old = json.loads(LEGACY_STATE_FILE.read_text() or "{}")
            if old:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                cur = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
                STATE_FILE.write_text(json.dumps({**old, **cur}, indent=1))
            LEGACY_STATE_FILE.unlink()
        except (OSError, ValueError):
            pass
    x = X11(display)
    p = Parker(x, display)
    n = len(p.records)
    p.restore_all()
    x.close()
    return n


# =================================================================================================
# Worker thread: keeps the target workspace parked and streams scaled frames
# =================================================================================================
class Worker(QObject):
    frame = pyqtSignal(object)          # dict: canvas, bg, items[(QImage, QRect)], scale
    desktops = pyqtSignal(object)       # dict: names, counts, current
    status = pyqtSignal(str)

    def __init__(self, display: str, target: int, own_pid: int):
        super().__init__()
        self.display, self.target, self.own_pid = display, target, own_pid
        self.view_w, self.view_h = 1280, 720
        self.fps = 5.0

    @pyqtSlot()
    def setup(self):
        self.x = X11(self.display)
        self.cap = ShmCapture(self.x)
        self.damage = DamageTracker(self.x)
        self.cache = {}      # frame -> (scaled QImage, scale, full size)
        self.last_emit = None
        self.parker = Parker(self.x, self.display)
        self.windows, self.last_list, self.bg, self.last_bg, self.desktop_wid = [], 0.0, None, 0.0, None
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(1000 / self.fps))

    @pyqtSlot(float)
    def set_fps(self, fps):
        self.fps = fps
        self.timer.start(int(1000 / fps))

    @pyqtSlot(int, int)
    def set_view(self, w, h):
        self.view_w, self.view_h = max(1, w), max(1, h)
        if hasattr(self, "cache"):
            self.cache.clear()
            self.last_bg = 0.0

    @pyqtSlot(int)
    def set_target(self, t):
        if t != self.target:
            self.target = t
            self.parker.restore_all()
            self.last_list = 0.0

    @pyqtSlot()
    def shutdown(self):
        self.timer.stop()
        self.parker.restore_all()
        self.damage.close()
        self.cap.close()
        self.x.close()

    @pyqtSlot(int)
    def activate(self, wid):
        """Release everything and jump to the real window."""
        self.parker.restore_all()
        _wmctrl("-i", "-a", hex(wid))

    # --- helpers --------------------------------------------------------------
    def _members(self, wins):
        out = []
        for i, w in enumerate(wins):
            if w.types & SKIP_TYPES or w.pid == self.own_pid:
                continue
            if "_NET_WM_STATE_HIDDEN" in w.states and not self.parker.is_parked(w.wid):
                continue  # minimized: not visible on its workspace either
            rec = self.parker.records.get(w.wid)
            desk = rec["desktop"] if rec else w.desktop
            if desk == self.target:
                out.append((rec["order"] if rec else i, w, rec))
        out.sort(key=lambda t: t[0])
        return out

    def _refresh(self, now):
        self.windows = self.x.windows()
        self.last_list = now
        current = self.x.current_desktop()
        names = self.x.desktop_names()
        counts = [0] * len(names)
        for w in self.windows:
            if w.types & SKIP_TYPES or "_NET_WM_STATE_HIDDEN" in w.states:
                continue
            rec = self.parker.records.get(w.wid)
            d = rec["desktop"] if rec else w.desktop
            if 0 <= d < len(counts):
                counts[d] += 1
        self.desktops.emit({"names": names, "counts": counts, "current": current})

        # Forget records of windows that were closed while parked.
        alive = {w.wid for w in self.windows}
        for wid in [k for k in self.parker.records if k not in alive]:
            self.parker.restore(wid)

        if current == self.target:
            if self.parker.records:  # user is looking at it: give the windows back
                self.parker.restore_all()
                self.windows = self.x.windows()
            self.status.emit("You are on this workspace — showing it directly")
            return
        to_park = [(o, w) for o, w, rec in self._members(self.windows) if rec is None and not w.viewable]
        for order, w in to_park:
            self.parker.park(w, order)
        if to_park:
            self.x.sync()
            time.sleep(0.05)
            self.windows = self.x.windows()
        n = len(self.parker.records)
        self.status.emit(f"Watching live · {n} window{'s' * (n != 1)} parked")

    # --- main loop --------------------------------------------------------------
    @pyqtSlot()
    def tick(self):
        now = time.monotonic()
        try:
            if now - self.last_list >= LIST_EVERY:
                self._refresh(now)
        except Exception as e:  # keep running; report
            self.status.emit(f"error: {e}")

        sw, sh = self.x.screen_size()
        s = min(self.view_w / sw, self.view_h / sh)

        if now - self.last_bg >= BG_EVERY:
            self.last_bg = now
            dw = next((w for w in self.windows if "_NET_WM_WINDOW_TYPE_DESKTOP" in w.types), None)
            img = self.cap.grab(dw.frame) if dw else None
            self.bg = img.scaled(max(1, int(sw * s)), max(1, int(sh * s)), Qt.IgnoreAspectRatio,
                                 Qt.SmoothTransformation) if img is not None else None

        dirty = self.damage.poll()
        items, live, changed = [], set(), False
        for _order, w, rec in self._members(self.windows):
            f = w.frame
            self.damage.track(f)
            cached = self.cache.get(f)
            if cached is None or f in dirty or cached[1] != s:
                self.damage.clean(f)  # before grabbing, so changes during the grab re-mark it
                img = self.cap.grab(f)
                if img is None:
                    continue
                full = (img.width(), img.height())
                scaled = img.scaled(max(1, int(full[0] * s)), max(1, int(full[1] * s)),
                                    Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                cached = self.cache[f] = (scaled, s, full)
                changed = True
            live.add(f)
            fx, fy = (rec["fx"], rec["fy"]) if rec else (w.fx, w.fy)
            img = cached[0]
            items.append((img, QRect(int(fx * s), int(fy * s), img.width(), img.height()), w.wid, w.title))
        for f in [f for f in self.cache if f not in live]:
            del self.cache[f]
        self.damage.keep_only(live)
        if self.bg is not None:
            live.update(w.frame for w in self.windows if "_NET_WM_WINDOW_TYPE_DESKTOP" in w.types)
        self.cap.keep_only(live)
        key = [(id(i[0]), i[1].getRect()) for i in items] + [id(self.bg)]
        if changed or key != self.last_emit:
            self.last_emit = key
            self.frame.emit({"canvas": (int(sw * s), int(sh * s)), "bg": self.bg, "items": items})


# =================================================================================================
# GUI
# =================================================================================================
class Canvas(QWidget):
    resized = pyqtSignal(int, int)
    activate = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.data = None
        self.message = "starting…"
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 180)
        self.setMouseTracking(True)

    def resizeEvent(self, ev):
        self.resized.emit(self.width(), self.height())

    def _origin(self):
        cw, ch = self.data["canvas"]
        return (self.width() - cw) // 2, (self.height() - ch) // 2

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(20, 20, 23))
        if not self.data:
            p.setPen(QColor(200, 200, 205))
            p.drawText(self.rect(), Qt.AlignCenter, self.message)
            return
        ox, oy = self._origin()
        cw, ch = self.data["canvas"]
        if self.data["bg"] is not None:
            p.drawImage(ox, oy, self.data["bg"])
        else:
            p.fillRect(ox, oy, cw, ch, QColor(45, 52, 64))
        p.setClipRect(ox, oy, cw, ch)
        for img, r, _wid, _t in self.data["items"]:
            p.drawImage(r.translated(ox, oy).topLeft(), img)
        if not self.data["items"]:
            p.setPen(QColor(230, 230, 235))
            f = QFont(); f.setPointSize(14); p.setFont(f)
            p.drawText(QRect(ox, oy, cw, ch), Qt.AlignCenter, "No windows on this workspace")

    def _hit(self, pos):
        if not self.data:
            return None
        ox, oy = self._origin()
        for _img, r, wid, title in reversed(self.data["items"]):
            if r.translated(ox, oy).contains(pos):
                return wid, title
        return None

    def mouseMoveEvent(self, ev):
        h = self._hit(ev.pos())
        self.setToolTip(f"{h[1]}\n(double-click to go to this window)" if h else "")

    def mouseDoubleClickEvent(self, ev):
        h = self._hit(ev.pos())
        if h:
            self.activate.emit(h[0])


class Viewer(QWidget):
    req_fps = pyqtSignal(float)
    req_view = pyqtSignal(int, int)
    req_target = pyqtSignal(int)
    req_activate = pyqtSignal(int)
    req_shutdown = pyqtSignal()

    def __init__(self, display: str, target: int, fps: float):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 860)
        self.target = target
        self._stamps = []
        self._names = []

        # Workspace selector: numbered buttons in 2 rows (6 per row for 12 workspaces).
        self.ws_group = QButtonGroup(self)
        self.ws_group.setExclusive(True)
        self.ws_group.idClicked.connect(self._ws_clicked)
        self.ws_grid = QGridLayout()
        self.ws_grid.setSpacing(3)
        self.ws_grid.setContentsMargins(0, 0, 0, 0)
        self.fps_box = QSpinBox()
        self.fps_box.setRange(1, 60)
        self.fps_box.setValue(int(fps))
        self.fps_box.setSuffix(" fps")
        self.fps_box.valueChanged.connect(lambda v: self.req_fps.emit(float(v)))
        go = QPushButton("Go to workspace")
        go.clicked.connect(lambda: subprocess.run(["wmctrl", "-s", str(self.target)]))
        self.status = QLabel("")
        self.status.setStyleSheet("color: palette(mid);")
        self.rate = QLabel("")

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 6)
        bar.addWidget(QLabel("Workspace:"))
        bar.addLayout(self.ws_grid)
        bar.addSpacing(8)
        bar.addWidget(self.fps_box)
        bar.addWidget(go)
        bar.addSpacing(12)
        bar.addWidget(self.status, 1)
        bar.addWidget(self.rate)

        self.canvas = Canvas()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.canvas, 1)

        self.thread = QThread(self)
        self.worker = Worker(display, target, os.getpid())
        self.worker.fps = float(fps)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.setup)
        self.worker.frame.connect(self._on_frame)
        self.worker.desktops.connect(self._on_desktops)
        self.worker.status.connect(self.status.setText)
        self.req_fps.connect(self.worker.set_fps)
        self.req_view.connect(self.worker.set_view)
        self.req_target.connect(self.worker.set_target)
        self.req_activate.connect(self.worker.activate)
        self.req_shutdown.connect(self.worker.shutdown, Qt.BlockingQueuedConnection)
        self.canvas.resized.connect(self.req_view)
        self.canvas.activate.connect(self.req_activate)
        self.thread.start()

        t = QTimer(self)
        t.timeout.connect(self._update_rate)
        t.start(500)
        self._closed = False

    def _ws_clicked(self, idx):
        if idx != self.target:
            self.target = idx
            self.req_target.emit(idx)
            self._apply_desktops()

    def _build_ws_buttons(self, n):
        for b in self.ws_group.buttons():
            self.ws_group.removeButton(b)
            b.deleteLater()
        cols = max(1, (n + 1) // 2)
        for i in range(n):
            b = QPushButton(str(i + 1))
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setFixedSize(34, 24)
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.ws_group.addButton(b, i)
            self.ws_grid.addWidget(b, i // cols, i % cols)

    def _on_desktops(self, d):
        if d != self._names:
            self._names = d
            self._apply_desktops()

    def _apply_desktops(self):
        d = self._names
        if not d:
            return
        names, counts, cur = d["names"], d["counts"], d["current"]
        if len(self.ws_group.buttons()) != len(names):
            self._build_ws_buttons(len(names))
        for i, b in enumerate(self.ws_group.buttons()):
            c = counts[i] if i < len(counts) else 0
            b.setChecked(i == self.target)
            f = b.font()
            f.setBold(i == cur)  # bold = the workspace you are on
            b.setFont(f)
            b.setToolTip(f"Workspace {names[i]}: {c} window{'s' * (c != 1)}"
                         + ("  (you are here)" if i == cur else ""))
        if self.target < len(names):
            self.setWindowTitle(f"{APP_NAME} — {names[self.target]}")

    def _on_frame(self, data):
        self.canvas.data = data
        self.canvas.update()
        self._stamps.append(time.monotonic())

    def _update_rate(self):
        now = time.monotonic()
        self._stamps = [t for t in self._stamps if now - t < 1.0]
        self.rate.setText(f"{len(self._stamps)} updates/s")

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key_F:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif k == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
        elif k == Qt.Key_T:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, not (self.windowFlags() & Qt.WindowStaysOnTopHint))
            self.show()
        elif k == Qt.Key_Q:
            self.close()
        else:
            super().keyPressEvent(ev)

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        if self.thread.isRunning():
            self.req_shutdown.emit()  # blocks until windows are restored
            self.thread.quit()
            self.thread.wait(3000)

    def closeEvent(self, ev):
        self.shutdown()
        super().closeEvent(ev)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live view of another XFCE workspace.")
    ap.add_argument("--workspace", "-w", type=int, help="workspace number to watch (1-based)")
    ap.add_argument("--fps", type=float, default=5)
    ap.add_argument("--restore", action="store_true", help="put back windows left parked by a crashed viewer")
    args = ap.parse_args()
    display = os.environ.get("DISPLAY", ":0")

    n = restore_leftovers(display)  # always clean up after a previous crash first
    if args.restore:
        print(f"Restored {n} window(s).")
        return 0
    if n:
        print(f"Restored {n} window(s) left parked by a previous run.")

    x = X11(display)
    cur, ndesk = x.current_desktop(), x.num_desktops()
    if args.workspace:
        target = max(0, min(ndesk - 1, args.workspace - 1))
    else:  # first other workspace that has windows
        busy = [w.desktop for w in x.windows() if w.desktop != STICKY and not (w.types & SKIP_TYPES)]
        target = next((d for d in range(ndesk) if d != cur and d in busy), (cur + 1) % ndesk)
    x.close()

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    v = Viewer(display, target, args.fps)
    app.aboutToQuit.connect(v.shutdown)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: app.quit())
    keepalive = QTimer()  # lets Python run signal handlers while Qt's loop is busy
    keepalive.start(250)
    keepalive.timeout.connect(lambda: None)
    v.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
