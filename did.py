#!/usr/bin/env python3
"""DesktopInDesktop: run a hidden, always-rendered virtual X desktop and watch it live.

    did.py start [--display :99] [--size 1920x1080] [--wm]
    did.py run   [--display :99] -- <command...>
    did.py view  [--display :99] [--fps 15]
    did.py list  [--display :99]
    did.py stop  [--display :99]
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".cache" / "desktopindesktop"
DEFAULT_DISPLAY = ":99"


def _num(display: str) -> str:
    return display.lstrip(":").split(".")[0]


def _socket(display: str) -> Path:
    return Path(f"/tmp/.X11-unix/X{_num(display)}")


def _state_file(display: str) -> Path:
    return STATE_DIR / f"display-{_num(display)}.json"


def _log_file(display: str, name: str) -> Path:
    return STATE_DIR / f"display-{_num(display)}-{name}.log"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _load_state(display: str) -> dict:
    try:
        return json.loads(_state_file(display).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(display: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(display).write_text(json.dumps(state, indent=2))


def _spawn(cmd, display: str, log_name: str) -> subprocess.Popen:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DISPLAY=display)
    # The hidden desktop has no Wayland/session bus of its own; keep the user's D-Bus so apps work normally.
    env.pop("WAYLAND_DISPLAY", None)
    log = open(_log_file(display, log_name), "ab")
    return subprocess.Popen(
        cmd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True
    )


def is_running(display: str) -> bool:
    pid = _load_state(display).get("xvfb_pid")
    return bool(pid and _alive(pid) and _socket(display).exists())


def cmd_start(args) -> int:
    display = args.display
    if is_running(display):
        print(f"Virtual desktop {display} is already running.")
        return 0
    if _socket(display).exists():
        print(f"error: {_socket(display)} exists — display {display} is used by another X server. "
              f"Pick another with --display.", file=sys.stderr)
        return 1
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        print("error: Xvfb not found. Install it with:  sudo apt install xvfb", file=sys.stderr)
        return 1
    try:
        w, h = (int(v) for v in args.size.lower().split("x"))
    except ValueError:
        print("error: --size must look like 1920x1080", file=sys.stderr)
        return 1

    proc = _spawn([xvfb, display, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"],
                  display, "xvfb")
    for _ in range(100):  # up to 10 s
        if _socket(display).exists():
            break
        if proc.poll() is not None:
            print(f"error: Xvfb exited (see {_log_file(display, 'xvfb')})", file=sys.stderr)
            return 1
        time.sleep(0.1)
    else:
        print("error: Xvfb did not come up in time", file=sys.stderr)
        proc.terminate()
        return 1
    time.sleep(0.3)

    state = {"display": display, "size": [w, h], "xvfb_pid": proc.pid}
    if args.wm:
        wm = shutil.which("xfwm4")
        if wm:
            state["wm_pid"] = _spawn([wm, f"--display={display}", "--compositor=off"], display, "wm").pid
        else:
            print("warning: xfwm4 not found; running without a window manager", file=sys.stderr)
    _save_state(display, state)
    print(f"Virtual desktop {display} started ({w}x{h}{', with xfwm4' if 'wm_pid' in state else ''}).")
    print(f"  launch apps:  {sys.argv[0]} run --display {display} -- xterm")
    print(f"  watch it:     {sys.argv[0]} view --display {display}")
    return 0


def cmd_run(args) -> int:
    if not args.command:
        print("error: give a command, e.g.  did.py run -- xterm", file=sys.stderr)
        return 1
    if not is_running(args.display):
        print(f"error: virtual desktop {args.display} is not running (did.py start)", file=sys.stderr)
        return 1
    cmd = args.command[1:] if args.command[0] == "--" else args.command
    try:
        proc = _spawn(cmd, args.display, "apps")
    except FileNotFoundError:
        print(f"error: command not found: {cmd[0]}", file=sys.stderr)
        return 1
    print(f"Started '{' '.join(cmd)}' on {args.display} (pid {proc.pid}).")
    return 0


def cmd_view(args) -> int:
    from viewer import run_viewer
    return run_viewer(args.display, args.fps)


def cmd_list(args) -> int:
    if not is_running(args.display):
        print(f"Virtual desktop {args.display} is not running.")
        return 1
    env = dict(os.environ, DISPLAY=args.display)
    if shutil.which("wmctrl") and "wm_pid" in _load_state(args.display):
        return subprocess.call(["wmctrl", "-l", "-p"], env=env)
    # No WM: wmctrl needs one, so fall back to the raw window tree.
    out = subprocess.run(["xwininfo", "-root", "-children"], env=env, capture_output=True, text=True)
    for line in out.stdout.splitlines():
        if line.strip().startswith("0x") and '"' in line:
            print(line.strip())
    return 0


def cmd_stop(args) -> int:
    state = _load_state(args.display)
    if not state:
        print(f"No state for {args.display}; nothing to stop.")
        return 0
    for key in ("wm_pid", "xvfb_pid"):
        pid = state.get(key)
        if pid and _alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(30):
                if not _alive(pid):
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, signal.SIGKILL)
    _state_file(args.display).unlink(missing_ok=True)
    print(f"Virtual desktop {args.display} stopped (apps on it lose their display and exit).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Hidden virtual desktop with a live view-only viewer.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--display", default=DEFAULT_DISPLAY, help="X display (default :99)")
        sp.set_defaults(func=fn)
        return sp

    sp = add("start", cmd_start, "start the virtual desktop")
    sp.add_argument("--size", default="1920x1080", help="resolution WxH (default 1920x1080)")
    sp.add_argument("--wm", action="store_true", help="run xfwm4 inside it (window borders/placement)")

    sp = add("run", cmd_run, "launch a program on the virtual desktop")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="-- command [args...]")

    sp = add("view", cmd_view, "open the live viewer window")
    sp.add_argument("--fps", type=float, default=15.0, help="frames per second (default 15)")

    add("list", cmd_list, "list windows on the virtual desktop")
    add("stop", cmd_stop, "stop the virtual desktop")

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
