"""Minimal ctypes bindings to libX11: EWMH window info and per-window pixel capture.

Each thread must use its own X11 instance (Xlib connections are not shared across threads).
"""
import ctypes
import ctypes.util
from dataclasses import dataclass, field

from PyQt5 import sip
from PyQt5.QtGui import QImage

_X = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")

c_ulong, c_long, c_int, c_uint, c_void_p = ctypes.c_ulong, ctypes.c_long, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p
Window = Atom = c_ulong
AnyPropertyType = 0
ZPixmap = 2
AllPlanes = 0xFFFFFFFF
IsViewable = 2


class XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int), ("border_width", c_int),
        ("depth", c_int), ("visual", c_void_p), ("root", Window), ("class_", c_int),
        ("bit_gravity", c_int), ("win_gravity", c_int), ("backing_store", c_int),
        ("backing_planes", c_ulong), ("backing_pixel", c_ulong), ("save_under", c_int),
        ("colormap", c_ulong), ("map_installed", c_int), ("map_state", c_int),
        ("all_event_masks", c_long), ("your_event_mask", c_long), ("do_not_propagate_mask", c_long),
        ("override_redirect", c_int), ("screen", c_void_p),
    ]


class _XImageFuncs(ctypes.Structure):
    _fields_ = [(n, c_void_p) for n in
                ("create_image", "destroy_image", "get_pixel", "put_pixel", "sub_image", "add_pixel")]


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", c_int), ("height", c_int), ("xoffset", c_int), ("format", c_int), ("data", c_void_p),
        ("byte_order", c_int), ("bitmap_unit", c_int), ("bitmap_bit_order", c_int), ("bitmap_pad", c_int),
        ("depth", c_int), ("bytes_per_line", c_int), ("bits_per_pixel", c_int),
        ("red_mask", c_ulong), ("green_mask", c_ulong), ("blue_mask", c_ulong), ("obdata", c_void_p),
        ("f", _XImageFuncs),
    ]


_X.XOpenDisplay.restype = c_void_p
_X.XOpenDisplay.argtypes = [ctypes.c_char_p]
_X.XCloseDisplay.argtypes = [c_void_p]
_X.XDefaultRootWindow.restype = Window
_X.XDefaultRootWindow.argtypes = [c_void_p]
_X.XInternAtom.restype = Atom
_X.XInternAtom.argtypes = [c_void_p, ctypes.c_char_p, c_int]
_X.XGetWindowProperty.argtypes = [
    c_void_p, Window, Atom, c_long, c_long, c_int, Atom, ctypes.POINTER(Atom), ctypes.POINTER(c_int),
    ctypes.POINTER(c_ulong), ctypes.POINTER(c_ulong), ctypes.POINTER(ctypes.c_void_p)]
_X.XFree.argtypes = [c_void_p]
_X.XGetWindowAttributes.argtypes = [c_void_p, Window, ctypes.POINTER(XWindowAttributes)]
_X.XQueryTree.argtypes = [c_void_p, Window, ctypes.POINTER(Window), ctypes.POINTER(Window),
                          ctypes.POINTER(ctypes.POINTER(Window)), ctypes.POINTER(c_uint)]
_X.XGetImage.restype = ctypes.POINTER(XImage)
_X.XGetImage.argtypes = [c_void_p, Window, c_int, c_int, c_uint, c_uint, c_ulong, c_int]
_X.XSync.argtypes = [c_void_p, c_int]

# Windows can vanish at any moment; swallow X protocol errors (BadWindow/BadMatch) instead of exiting.
_ERROR_HANDLER = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)(lambda _d, _e: 0)
_X.XSetErrorHandler(_ERROR_HANDLER)
_DESTROY = ctypes.CFUNCTYPE(c_int, ctypes.POINTER(XImage))


@dataclass
class WinInfo:
    wid: int                 # client window id
    frame: int               # WM frame (top-level child of root; includes decorations)
    title: str
    desktop: int             # 0xFFFFFFFF = sticky
    states: set = field(default_factory=set)
    types: set = field(default_factory=set)
    pid: int = 0
    viewable: bool = False
    fx: int = 0              # frame geometry in root coordinates
    fy: int = 0
    fw: int = 0
    fh: int = 0
    cx: int = 0              # client geometry in root coordinates
    cy: int = 0
    cw: int = 0
    ch: int = 0


class X11:
    def __init__(self, display: str = None):
        self.dpy = _X.XOpenDisplay(display.encode() if display else None)
        if not self.dpy:
            raise RuntimeError(f"cannot open X display {display or ''}")
        self.root = _X.XDefaultRootWindow(self.dpy)
        self._atoms = {}

    def close(self):
        if self.dpy:
            _X.XCloseDisplay(self.dpy)
            self.dpy = None

    def atom(self, name: str) -> int:
        a = self._atoms.get(name)
        if a is None:
            a = self._atoms[name] = _X.XInternAtom(self.dpy, name.encode(), 0)
        return a

    def atom_name(self, atom: int) -> str:
        for k, v in self._atoms.items():
            if v == atom:
                return k
        _X.XGetAtomName.restype = c_void_p
        _X.XGetAtomName.argtypes = [c_void_p, Atom]
        p = _X.XGetAtomName(self.dpy, atom)
        if not p:
            return ""
        name = ctypes.string_at(p).decode(errors="replace")
        _X.XFree(p)
        self._atoms[name] = atom
        return name

    # --- properties ---------------------------------------------------------
    def _prop(self, win: int, name: str):
        typ, fmt, n, after, data = Atom(), c_int(), c_ulong(), c_ulong(), ctypes.c_void_p()
        st = _X.XGetWindowProperty(self.dpy, win, self.atom(name), 0, 1 << 20, 0, AnyPropertyType,
                                   ctypes.byref(typ), ctypes.byref(fmt), ctypes.byref(n),
                                   ctypes.byref(after), ctypes.byref(data))
        if st != 0 or not data.value:
            return None, None
        try:
            if fmt.value == 32:  # format-32 data is returned as C longs
                return fmt.value, list((c_ulong * n.value).from_address(data.value))
            if fmt.value == 8:
                return fmt.value, ctypes.string_at(data.value, n.value)
            return fmt.value, list((ctypes.c_ushort * n.value).from_address(data.value))
        finally:
            _X.XFree(data)

    def cardinals(self, win: int, name: str) -> list:
        fmt, v = self._prop(win, name)
        return v if fmt == 32 else []

    def cardinal(self, win: int, name: str, default=None):
        v = self.cardinals(win, name)
        return v[0] if v else default

    def string(self, win: int, name: str) -> str:
        fmt, v = self._prop(win, name)
        return v.decode("utf-8", errors="replace") if fmt == 8 else ""

    # --- desktop-level info -------------------------------------------------
    def num_desktops(self) -> int:
        return self.cardinal(self.root, "_NET_NUMBER_OF_DESKTOPS", 1)

    def current_desktop(self) -> int:
        return self.cardinal(self.root, "_NET_CURRENT_DESKTOP", 0)

    def desktop_names(self) -> list:
        fmt, v = self._prop(self.root, "_NET_DESKTOP_NAMES")
        names = v.decode("utf-8", errors="replace").split("\0") if fmt == 8 else []
        n = self.num_desktops()
        return [(names[i] if i < len(names) and names[i] else str(i + 1)) for i in range(n)]

    def screen_size(self) -> tuple:
        a = self.attributes(self.root)
        return (a.width, a.height) if a else (1920, 1080)

    def stacking(self) -> list:
        """Managed client windows, bottom-to-top."""
        return self.cardinals(self.root, "_NET_CLIENT_LIST_STACKING") or self.cardinals(self.root, "_NET_CLIENT_LIST")

    # --- window-level info --------------------------------------------------
    def attributes(self, win: int):
        a = XWindowAttributes()
        return a if _X.XGetWindowAttributes(self.dpy, win, ctypes.byref(a)) else None

    def parent_chain_top(self, win: int) -> int:
        """Walk up to the ancestor that is a direct child of root (the WM frame)."""
        cur = win
        for _ in range(16):
            root, parent = Window(), Window()
            children, n = ctypes.POINTER(Window)(), c_uint()
            if not _X.XQueryTree(self.dpy, cur, ctypes.byref(root), ctypes.byref(parent),
                                 ctypes.byref(children), ctypes.byref(n)):
                return win
            if children:
                _X.XFree(children)
            if parent.value in (0, root.value):
                return cur
            cur = parent.value
        return cur

    def abs_origin(self, win: int) -> tuple:
        """Absolute root coordinates of a window's origin, by summing parent offsets."""
        x = y = 0
        cur = win
        for _ in range(16):
            a = self.attributes(cur)
            if not a:
                break
            x += a.x + a.border_width
            y += a.y + a.border_width
            root, parent = Window(), Window()
            children, n = ctypes.POINTER(Window)(), c_uint()
            if not _X.XQueryTree(self.dpy, cur, ctypes.byref(root), ctypes.byref(parent),
                                 ctypes.byref(children), ctypes.byref(n)):
                break
            if children:
                _X.XFree(children)
            if parent.value in (0, root.value):
                break
            cur = parent.value
        return x, y

    def info(self, wid: int):
        ca = self.attributes(wid)
        if not ca:
            return None
        frame = self.parent_chain_top(wid)
        fa = self.attributes(frame) or ca
        cx, cy = self.abs_origin(wid)
        return WinInfo(
            wid=wid, frame=frame,
            title=self.string(wid, "_NET_WM_NAME") or self.string(wid, "WM_NAME"),
            desktop=self.cardinal(wid, "_NET_WM_DESKTOP", 0),
            states={self.atom_name(a) for a in self.cardinals(wid, "_NET_WM_STATE")},
            types={self.atom_name(a) for a in self.cardinals(wid, "_NET_WM_WINDOW_TYPE")},
            pid=self.cardinal(wid, "_NET_WM_PID", 0),
            viewable=fa.map_state == IsViewable and ca.map_state == IsViewable,
            fx=fa.x, fy=fa.y, fw=fa.width, fh=fa.height,
            cx=cx, cy=cy, cw=ca.width, ch=ca.height,
        )

    def windows(self) -> list:
        out = []
        for w in self.stacking():
            i = self.info(w)
            if i:
                out.append(i)
        return out

    # --- capture --------------------------------------------------------------
    def grab(self, win: int):
        """Grab a window's pixels (works off-screen thanks to the compositor). Returns QImage or None."""
        a = self.attributes(win)
        if not a or a.map_state != IsViewable or a.width <= 0 or a.height <= 0:
            return None
        p = _X.XGetImage(self.dpy, win, 0, 0, a.width, a.height, AllPlanes, ZPixmap)
        if not p:
            return None
        try:
            im = p.contents
            if im.bits_per_pixel != 32:
                return None
            buf = ctypes.string_at(im.data, im.bytes_per_line * im.height)
            return QImage(buf, im.width, im.height, im.bytes_per_line, QImage.Format_RGB32).copy()
        finally:
            _DESTROY(p.contents.f.destroy_image)(p)

    def sync(self):
        _X.XSync(self.dpy, 0)


# --- MIT-SHM fast capture -------------------------------------------------------
_Xext = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xext") or "libXext.so.6")
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
IPC_PRIVATE, IPC_CREAT, IPC_RMID = 0, 0o1000, 0


_xc_path = ctypes.util.find_library("Xcomposite")
_Xc = ctypes.cdll.LoadLibrary(_xc_path) if _xc_path else None
if _Xc:
    _Xc.XCompositeNameWindowPixmap.restype = c_ulong
    _Xc.XCompositeNameWindowPixmap.argtypes = [c_void_p, Window]
_X.XFreePixmap.argtypes = [c_void_p, c_ulong]


class XShmSegmentInfo(ctypes.Structure):
    _fields_ = [("shmseg", c_ulong), ("shmid", c_int), ("shmaddr", c_void_p), ("readOnly", c_int)]


_libc.shmget.argtypes = [c_int, ctypes.c_size_t, c_int]
_libc.shmat.restype = c_void_p
_libc.shmat.argtypes = [c_int, c_void_p, c_int]
_libc.shmdt.argtypes = [c_void_p]
_libc.shmctl.argtypes = [c_int, c_int, c_void_p]
_Xext.XShmQueryExtension.argtypes = [c_void_p]
_Xext.XShmCreateImage.restype = ctypes.POINTER(XImage)
_Xext.XShmCreateImage.argtypes = [c_void_p, c_void_p, c_uint, c_int, c_void_p,
                                  ctypes.POINTER(XShmSegmentInfo), c_uint, c_uint]
_Xext.XShmAttach.argtypes = [c_void_p, ctypes.POINTER(XShmSegmentInfo)]
_Xext.XShmDetach.argtypes = [c_void_p, ctypes.POINTER(XShmSegmentInfo)]
_Xext.XShmGetImage.argtypes = [c_void_p, Window, ctypes.POINTER(XImage), c_int, c_int, c_ulong]


class _ShmBuf:
    def __init__(self, x: "X11", attrs: XWindowAttributes):
        self.x, self.key = x, (attrs.width, attrs.height, attrs.depth, attrs.visual)
        self.info = XShmSegmentInfo()
        self.img = _Xext.XShmCreateImage(x.dpy, attrs.visual, attrs.depth, ZPixmap, None,
                                         ctypes.byref(self.info), attrs.width, attrs.height)
        if not self.img:
            raise OSError("XShmCreateImage failed")
        im = self.img.contents
        size = im.bytes_per_line * im.height
        self.info.shmid = _libc.shmget(IPC_PRIVATE, size, IPC_CREAT | 0o600)
        if self.info.shmid < 0:
            raise OSError(ctypes.get_errno(), "shmget failed")
        addr = _libc.shmat(self.info.shmid, None, 0)
        if addr in (None, ctypes.c_void_p(-1).value):
            _libc.shmctl(self.info.shmid, IPC_RMID, None)
            raise OSError(ctypes.get_errno(), "shmat failed")
        self.info.shmaddr = im.data = addr
        self.info.readOnly = 0
        _Xext.XShmAttach(x.dpy, ctypes.byref(self.info))
        x.sync()
        _libc.shmctl(self.info.shmid, IPC_RMID, None)  # freed automatically once both sides detach

    def free(self):
        if self.img:
            _Xext.XShmDetach(self.x.dpy, ctypes.byref(self.info))
            self.x.sync()
            self.img.contents.data = None
            _DESTROY(self.img.contents.f.destroy_image)(self.img)
            _libc.shmdt(self.info.shmaddr)
            self.img = None


class ShmCapture:
    """Per-window shared-memory capture with buffer reuse. Falls back to XGetImage."""

    def __init__(self, x: X11):
        self.x = x
        self.ok = bool(_Xext.XShmQueryExtension(x.dpy))
        self.bufs = {}

    def grab(self, win: int):
        a = self.x.attributes(win)
        if not a or a.map_state != IsViewable or a.width <= 0 or a.height <= 0:
            self.drop(win)
            return None
        if not self.ok:
            return self.x.grab(win)
        key = (a.width, a.height, a.depth, a.visual)
        buf = self.bufs.get(win)
        if buf is None or buf.key != key:
            self.drop(win)
            try:
                buf = self.bufs[win] = _ShmBuf(self.x, a)
            except OSError:
                return self.x.grab(win)
        # Read from the compositor's off-screen pixmap of the window: unlike the window itself it has no
        # "must be on screen" restriction, so windows parked mostly off-screen can be captured.
        pm = _Xc.XCompositeNameWindowPixmap(self.x.dpy, win) if _Xc else 0
        ok = bool(pm) and _Xext.XShmGetImage(self.x.dpy, pm, buf.img, 0, 0, AllPlanes)
        if pm:
            _X.XFreePixmap(self.x.dpy, pm)
        if not ok:  # compositor off / not redirected: fall back to the window (on-screen part must exist)
            if not _Xext.XShmGetImage(self.x.dpy, win, buf.img, 0, 0, AllPlanes):
                img = self.x.grab(win)
                return img
        im = buf.img.contents
        if im.bits_per_pixel != 32:
            return None
        # Zero-copy view of the shared buffer: only valid until the next grab()/drop() of this window,
        # so callers must scale/copy it right away.
        return QImage(sip.voidptr(im.data), im.width, im.height, im.bytes_per_line, QImage.Format_RGB32)

    def drop(self, win: int):
        b = self.bufs.pop(win, None)
        if b:
            b.free()

    def keep_only(self, wins):
        for w in list(self.bufs):
            if w not in wins:
                self.drop(w)

    def close(self):
        for w in list(self.bufs):
            self.drop(w)


# --- XDamage: only re-grab windows whose contents changed ----------------------
_xd_path = ctypes.util.find_library("Xdamage")
_Xd = ctypes.cdll.LoadLibrary(_xd_path) if _xd_path else None
if _Xd:
    _Xd.XDamageQueryExtension.argtypes = [c_void_p, ctypes.POINTER(c_int), ctypes.POINTER(c_int)]
    _Xd.XDamageCreate.restype = c_ulong
    _Xd.XDamageCreate.argtypes = [c_void_p, Window, c_int]
    _Xd.XDamageDestroy.argtypes = [c_void_p, c_ulong]
    _Xd.XDamageSubtract.argtypes = [c_void_p, c_ulong, c_ulong, c_ulong]
_X.XPending.argtypes = [c_void_p]
_X.XNextEvent.argtypes = [c_void_p, c_void_p]
XDamageReportNonEmpty = 3
XDamageNotify = 0


class XDamageNotifyEvent(ctypes.Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                ("drawable", Window), ("damage", c_ulong), ("level", c_int), ("more", c_int)]


class DamageTracker:
    """Reports which tracked windows changed since they were last marked clean."""

    def __init__(self, x: X11):
        self.x = x
        self.handles = {}
        self.dirty = set()
        ev, err = c_int(), c_int()
        self.ok = bool(_Xd and _Xd.XDamageQueryExtension(x.dpy, ctypes.byref(ev), ctypes.byref(err)))
        self.event_type = ev.value + XDamageNotify
        self._buf = ctypes.create_string_buffer(256)  # >= sizeof(XEvent)

    def track(self, win: int):
        if win in self.handles:
            return
        self.handles[win] = _Xd.XDamageCreate(self.x.dpy, win, XDamageReportNonEmpty) if self.ok else 0
        self.dirty.add(win)

    def untrack(self, win: int):
        h = self.handles.pop(win, None)
        if h:
            _Xd.XDamageDestroy(self.x.dpy, h)
        self.dirty.discard(win)

    def keep_only(self, wins):
        for w in list(self.handles):
            if w not in wins:
                self.untrack(w)

    def poll(self) -> set:
        """Drain X events; returns the set of dirty windows (tracking is cumulative until clean())."""
        if not self.ok:
            self.dirty.update(self.handles)
            return self.dirty
        while _X.XPending(self.x.dpy):
            _X.XNextEvent(self.x.dpy, self._buf)
            ev = XDamageNotifyEvent.from_buffer(self._buf)
            if ev.type == self.event_type and ev.drawable in self.handles:
                self.dirty.add(ev.drawable)
        return self.dirty

    def clean(self, win: int):
        """Call right BEFORE grabbing, so changes during the grab re-mark the window."""
        h = self.handles.get(win)
        if h:
            _Xd.XDamageSubtract(self.x.dpy, h, 0, 0)
        self.dirty.discard(win)

    def close(self):
        for w in list(self.handles):
            self.untrack(w)
