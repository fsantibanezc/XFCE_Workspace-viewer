"""View-only live viewer for an X display (e.g. the hidden Xvfb desktop).

Keys:  F fullscreen · +/- FPS · 1 native size · T always-on-top · Q/Esc close
"""
import sys
import time

import mss
from PyQt5.QtCore import QObject, QRect, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QImage, QPainter
from PyQt5.QtWidgets import QApplication, QWidget

MIN_FPS, MAX_FPS = 1.0, 60.0


class Grabber(QObject):
    """Lives in a worker thread; grabs one frame per tick from the target display."""

    frame = pyqtSignal(QImage)
    failed = pyqtSignal(str)

    def __init__(self, display: str):
        super().__init__()
        self.display = display
        self._sct = None
        self._timer = None

    @pyqtSlot()
    def setup(self):
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self.grab)

    @pyqtSlot(float)
    def set_fps(self, fps: float):
        self._timer.start(int(1000 / fps))

    @pyqtSlot()
    def stop(self):
        if self._timer:
            self._timer.stop()
        self._close()

    def _close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None

    @pyqtSlot()
    def grab(self):
        try:
            if self._sct is None:
                self._sct = getattr(mss, "MSS", mss.mss)(display=self.display)
            mon = self._sct.monitors[0]  # whole X screen
            shot = self._sct.grab(mon)
            # shot.raw is BGRA, which is QImage.Format_RGB32 on little-endian. .copy() detaches from the buffer.
            img = QImage(shot.raw, shot.width, shot.height, shot.width * 4, QImage.Format_RGB32).copy()
            self.frame.emit(img)
        except Exception as e:  # display down / not started yet: drop the connection and retry next tick
            self._close()
            self.failed.emit(str(e) or e.__class__.__name__)


class Viewer(QWidget):
    request_fps = pyqtSignal(float)
    request_stop = pyqtSignal()

    def __init__(self, display: str, fps: float):
        super().__init__()
        self.display = display
        self.fps = max(MIN_FPS, min(MAX_FPS, fps))
        self.image = None
        self.error = "connecting…"
        self._stamps = []
        self.measured = 0.0

        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.resize(1280, 720)
        self._update_title()

        self.thread = QThread(self)
        self.grabber = Grabber(display)
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.setup)
        self.grabber.frame.connect(self.on_frame)
        self.grabber.failed.connect(self.on_failed)
        self.request_fps.connect(self.grabber.set_fps)
        self.request_stop.connect(self.grabber.stop, Qt.BlockingQueuedConnection)
        self.thread.start()
        QTimer.singleShot(0, lambda: self.request_fps.emit(self.fps))

        self._title_timer = QTimer(self)
        self._title_timer.timeout.connect(self._tick_title)
        self._title_timer.start(500)

    # --- frames -------------------------------------------------------------
    def on_frame(self, img: QImage):
        self.image, self.error = img, None
        self._stamps.append(time.monotonic())
        self.update()

    def on_failed(self, msg: str):
        if self.image is not None or self.error != msg:
            self.image, self.error = None, msg
            self.update()

    def _tick_title(self):
        now = time.monotonic()
        self._stamps = [t for t in self._stamps if now - t < 1.0]
        self.measured = len(self._stamps)
        self._update_title()

    def _update_title(self):
        size = f"{self.image.width()}x{self.image.height()}" if self.image is not None else "offline"
        self.setWindowTitle(
            f"Desktop {self.display} — {size} — {self.measured:.0f}/{self.fps:g} fps (view only)"
        )

    # --- painting -----------------------------------------------------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(24, 24, 27))
        if self.image is None:
            p.setPen(QColor(200, 200, 205))
            f = QFont()
            f.setPointSize(13)
            p.setFont(f)
            p.drawText(
                self.rect(), Qt.AlignCenter | Qt.TextWordWrap,
                f"Waiting for virtual desktop {self.display}\n\n"
                f"Start it with:  python did.py start --display {self.display} --wm\n\n"
                f"({self.error})",
            )
            return
        iw, ih = self.image.width(), self.image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = int(iw * scale), int(ih * scale)
        target = QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)
        p.setRenderHint(QPainter.SmoothPixmapTransform, scale != 1.0)
        p.drawImage(target, self.image)

    # --- keys ---------------------------------------------------------------
    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (Qt.Key_Q, Qt.Key_Escape):
            if self.isFullScreen() and k == Qt.Key_Escape:
                self.showNormal()
            else:
                self.close()
        elif k == Qt.Key_F:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif k in (Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus):
            step = 5 if self.fps >= 10 else 1
            self.fps += step if k != Qt.Key_Minus else -step
            self.fps = max(MIN_FPS, min(MAX_FPS, self.fps))
            self.request_fps.emit(self.fps)
            self._update_title()
        elif k == Qt.Key_1 and self.image is not None:
            self.showNormal()
            self.resize(self.image.width(), self.image.height())
        elif k == Qt.Key_T:
            on_top = not (self.windowFlags() & Qt.WindowStaysOnTopHint)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, on_top)
            self.show()
        else:
            super().keyPressEvent(ev)

    def closeEvent(self, ev):
        self.request_stop.emit()
        self.thread.quit()
        self.thread.wait(2000)
        super().closeEvent(ev)


def run_viewer(display: str, fps: float = 15.0) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("XFCE Workspace Viewer — hidden desktop")
    v = Viewer(display, fps)
    v.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(run_viewer(sys.argv[1] if len(sys.argv) > 1 else ":99"))
