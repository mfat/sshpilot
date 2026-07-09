#!/usr/bin/env python3
"""Local web-terminal server for sshPilot's PyXterm.js backend.

Transport is a plain WebSocket (via :mod:`simple_websocket`) rather than
Socket.IO, and the xterm.js assets are served locally (system
``/usr/share/javascript/xterm`` when present, otherwise a bundled copy) instead
of from a CDN. This keeps the backend self-contained and offline-capable, with
no third-party network requests -- a requirement for distribution packaging.

WebSocket protocol (JSON text frames):
  client -> server: {"type": "input",  "data": "<keystrokes>"}
                    {"type": "resize", "rows": <int>, "cols": <int>}
  server -> client: {"type": "output", "data": "<pty output>"}
"""
import argparse
import fcntl
import json
import logging
import os
import pty
import select
import shlex
import struct
import subprocess
import sys
import termios
import threading
import time

from flask import Flask, render_template, request, send_from_directory
import simple_websocket

logging.getLogger("werkzeug").setLevel(logging.ERROR)

__version__ = "0.6.0"

# Allow template/static folders to be overridden (e.g. Flatpak writable dirs).
template_folder = os.environ.get("PYXTERMJS_TEMPLATE_FOLDER", ".")
static_folder = os.environ.get("PYXTERMJS_STATIC_FOLDER", ".")

app = Flask(
    __name__,
    template_folder=template_folder,
    static_folder=static_folder,
    static_url_path="",
)
app.config["cmd"] = ["bash"]

# One PTY per server process, streamed to whichever WebSocket is connected.
_state = {
    "fd": None,
    "child_pid": None,
    "ws": None,
    "reader_started": False,
    "lock": threading.Lock(),
}


def xterm_asset_dir():
    """Directory holding xterm.js assets (xterm.js, xterm.css, addons/...).

    Prefers an explicit override, then the Debian system copy shipped by
    ``libjs-xterm``, and finally a copy bundled next to this module.
    """
    env = os.environ.get("PYXTERMJS_ASSETS_DIR")
    if env and os.path.isdir(env):
        return env
    system = "/usr/share/javascript/xterm"
    if os.path.isdir(system):
        return system
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "xterm")


def set_winsize(fd, row, col, xpix=0, ypix=0):
    winsize = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/xterm/<path:filename>")
def xterm_assets(filename):
    """Serve xterm.js core/addons/css locally (no CDN)."""
    return send_from_directory(xterm_asset_dir(), filename)


def _read_and_forward_pty_output():
    """Forward PTY output to the currently connected WebSocket."""
    max_read_bytes = 1024 * 20
    while True:
        fd = _state["fd"]
        if fd is None:
            time.sleep(0.02)
            continue
        try:
            data_ready, _, _ = select.select([fd], [], [], 0.05)
        except (OSError, ValueError):
            break
        if not data_ready:
            continue
        try:
            output = os.read(fd, max_read_bytes)
        except OSError:
            break
        if not output:
            break
        ws = _state["ws"]
        if ws is not None:
            try:
                ws.send(json.dumps({"type": "output", "data": output.decode(errors="ignore")}))
            except Exception:
                # Client went away; keep the PTY alive for the next connection.
                pass


@app.route("/pty", websocket=True)
def pty_ws():
    """WebSocket endpoint: bridge the browser terminal to the child PTY."""
    ws = simple_websocket.Server(request.environ)
    with _state["lock"]:
        _state["ws"] = ws
        if _state["child_pid"] is None:
            child_pid, fd = pty.fork()
            if child_pid == 0:
                # Child: become the requested command, then exit the fork.
                try:
                    subprocess.run(app.config["cmd"])
                finally:
                    os._exit(0)
            _state["fd"] = fd
            _state["child_pid"] = child_pid
            set_winsize(fd, 50, 50)
            logging.info("child pid is %s", child_pid)
        if not _state["reader_started"]:
            _state["reader_started"] = True
            threading.Thread(target=_read_and_forward_pty_output, daemon=True).start()
    try:
        while True:
            message = ws.receive()
            if message is None:
                break
            try:
                obj = json.loads(message)
            except (ValueError, TypeError):
                continue
            kind = obj.get("type")
            if kind == "input" and _state["fd"] is not None:
                os.write(_state["fd"], obj.get("data", "").encode())
            elif kind == "resize" and _state["fd"] is not None:
                set_winsize(_state["fd"], int(obj["rows"]), int(obj["cols"]))
    except simple_websocket.ConnectionClosed:
        pass
    finally:
        if _state["ws"] is ws:
            _state["ws"] = None
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="A local web terminal for sshPilot (native WebSocket transport).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--port", default=5000, type=int, help="port to run server on")
    parser.add_argument("--host", default="127.0.0.1", help="host to run server on")
    parser.add_argument("--debug", action="store_true", help="debug the server")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--command", default="bash", help="Command to run in the terminal")
    parser.add_argument(
        "--cmd-args",
        default="",
        help="arguments to pass to command (i.e. --cmd-args='arg1 arg2 --flag')",
    )
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return 0
    app.config["cmd"] = [args.command] + shlex.split(args.cmd_args)
    logging.basicConfig(
        format="pyxtermjs > %(levelname)s (%(funcName)s:%(lineno)s) %(message)s",
        stream=sys.stdout,
        level=logging.DEBUG if args.debug else logging.INFO,
    )
    logging.info("serving on http://%s:%s", args.host, args.port)
    # threaded=True lets the long-lived WebSocket run alongside asset requests.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
