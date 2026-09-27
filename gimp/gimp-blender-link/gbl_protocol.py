# GIMP Link for Blender: the protocol and file conventions shared by the
# Blender add-on and the GIMP plug-in. Pure Python (no bpy, no Gimp).
# The copies in blender/gimp_link/ and gimp/gimp-blender-link/ must stay
# identical; tests/run.sh checks that.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import errno
import hmac
import json
import os
import secrets
import select
import socket
import sys
import time

PROTOCOL = 1
FORMAT = "gimp-blender-link"
DEFAULT_GIMP_PORT = 29317
MAX_LINE = 1 << 20
LISTENER_FILE = "gimp-listener.json"
SETTINGS_FILE = "gimp-settings.json"
PENDING_DIR = "pending"
PENDING_MAX_AGE = 24 * 3600
# commands that need no token
OPEN_COMMANDS = ("ping",)


class ProtocolError(Exception):
    pass


# ---------------------------------------------------------------- paths

def link_dir():
    """The folder both apps use to find each other.

    A literal path under the home folder: a Flatpak sets its own
    XDG_DATA_HOME per app, so that variable would differ between the
    Blender and the GIMP Flatpak. GIMP_BLENDER_LINK_DIR overrides it."""
    env = os.environ.get("GIMP_BLENDER_LINK_DIR")
    if env:
        return env
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(base, "gimp-blender-link")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "gimp-blender-link")
    return os.path.join(home, ".local", "share", "gimp-blender-link")


def gimp_port():
    try:
        return int(os.environ.get("GIMP_BLENDER_LINK_PORT", DEFAULT_GIMP_PORT))
    except ValueError:
        return DEFAULT_GIMP_PORT


def is_temporary_path(path):
    """True for paths the other Flatpak may not see (a private /tmp)."""
    p = os.path.realpath(path)
    return p == "/tmp" or p.startswith("/tmp/") or p.startswith("/var/tmp/")


# ---------------------------------------------------------------- files

def new_token():
    return secrets.token_hex(16)


def new_link_id():
    return secrets.token_hex(8)


def write_atomic(path, data, mode=0o644):
    """Write bytes to path through a temporary file in the same folder
    and os.replace, so that a reader never sees a partial file."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj, mode=0o600):
    write_atomic(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"), mode)


def read_json(path):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def stat_key(path):
    """(mtime_ns, size, inode) of a file, or None if it is missing."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def stat_to_json(key):
    return None if key is None else list(key)


def stat_from_json(v):
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        return None
    try:
        return tuple(int(x) for x in v)
    except (TypeError, ValueError):
        return None


def looks_complete(path):
    """A cheap check that an image file was written to the end.

    PNG must end with its IEND chunk; OpenEXR must start with its magic
    number. Other formats: only that the file is not empty. Returns
    False for files that are certainly incomplete."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(8)
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                if size < 8 + 25 + 12:
                    return False
                f.seek(size - 12)
                return f.read(12) == b"\x00\x00\x00\x00IEND\xaeB`\x82"
            if path.lower().endswith(".exr"):
                return head[:4] == b"\x76\x2f\x31\x01"
            return size > 0
    except OSError:
        return False


class FileWatch:
    """Decides when a changed file may be reloaded.

    seen: the stat last taken in (loaded, or written by us).
    A new stat is taken when it is stable on two polls in a row and
    looks complete, or at once when the writer announced exactly that
    stat (announce()). A file that disappears is not a change: an
    atomic replace can be caught between unlink and rename on some
    systems, and a missing file cannot be reloaded anyway."""

    def __init__(self, seen=None):
        self.seen = seen
        self.candidate = None
        self.announced = None

    def mark_seen(self, key):
        self.seen = key
        self.candidate = None
        if self.announced == key:
            self.announced = None

    def announce(self, key):
        self.announced = key

    def poll(self, key, complete=True):
        """key: the current stat_key. Returns True if it should be
        reloaded now; the caller then calls mark_seen(key)."""
        if key is None or key == self.seen:
            self.candidate = None
            return False
        if self.announced is not None and key == self.announced and complete:
            return True
        if key == self.candidate and complete:
            return True
        self.candidate = key
        return False


# ---------------------------------------------------------------- messages

def encode(obj):
    data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > MAX_LINE:
        raise ProtocolError("message too long")
    return data


def decode(line):
    if len(line) > MAX_LINE:
        raise ProtocolError("message too long")
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("not JSON")
    if not isinstance(obj, dict):
        raise ProtocolError("not a JSON object")
    return obj


def token_ok(given, expected):
    if not isinstance(given, str) or not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def handle_line(line, token, handlers, app):
    """Checks one request line and runs its handler.

    handlers: {cmd: function(msg) -> dict}. Returns the reply dict; a
    handler's exception becomes an error reply."""
    try:
        msg = decode(line)
    except ProtocolError as e:
        return {"ok": False, "error": str(e)}
    cmd = msg.get("cmd")
    if msg.get("v") != PROTOCOL:
        return {"ok": False, "error": "unsupported protocol version", "v": PROTOCOL}
    if cmd == "ping":
        return {"ok": True, "app": app, "v": PROTOCOL}
    if not token_ok(msg.get("token"), token):
        return {"ok": False, "error": "bad token"}
    fn = handlers.get(cmd)
    if fn is None:
        return {"ok": False, "error": "unknown command"}
    try:
        reply = fn(msg)
    except Exception as e:  # reported to the other side, not raised here
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    if reply is None:
        reply = {}
    reply.setdefault("ok", True)
    return reply


def request(port, msg, token=None, timeout=5.0, host="127.0.0.1"):
    """Sends one request and returns the reply dict. Raises OSError when
    nobody listens, ProtocolError on a broken reply."""
    msg = dict(msg)
    msg.setdefault("v", PROTOCOL)
    if token is not None:
        msg["token"] = token
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(encode(msg))
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_LINE:
                raise ProtocolError("reply too long")
    if not data:
        raise ProtocolError("no reply")
    return decode(data.strip())


class LineServer:
    """A non-blocking one-line-per-connection server, polled from a
    timer (Blender's main thread) or a loop (tests). Never blocks for
    long: reads what is there, answers complete lines, drops slow or
    oversized clients."""

    def __init__(self, token, handlers, app, port=0, host="127.0.0.1", client_timeout=5.0):
        self.token = token
        self.handlers = handlers
        self.app = app
        self.client_timeout = client_timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.clients = {}  # socket -> [buffer, started]

    def close(self):
        for c in list(self.clients):
            self._drop(c)
        try:
            self.sock.close()
        except OSError:
            pass

    def _drop(self, c):
        self.clients.pop(c, None)
        try:
            c.close()
        except OSError:
            pass

    def poll(self, timeout=0.0):
        """Serves what is ready. Returns the number of requests answered."""
        answered = 0
        try:
            ready, _, _ = select.select([self.sock] + list(self.clients), [], [], timeout)
        except (OSError, ValueError):
            return 0
        for r in ready:
            if r is self.sock:
                while True:
                    try:
                        c, _ = self.sock.accept()
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        break
                    c.setblocking(False)
                    self.clients[c] = [b"", time.monotonic()]
                continue
            buf = self.clients.get(r)
            if buf is None:
                continue
            try:
                chunk = r.recv(65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                self._drop(r)
                continue
            if not chunk:
                self._drop(r)
                continue
            buf[0] += chunk
            if b"\n" in buf[0] or len(buf[0]) > MAX_LINE:
                line = buf[0].split(b"\n", 1)[0]
                reply = handle_line(line, self.token, self.handlers, self.app)
                self._reply(r, reply)
                answered += 1
        now = time.monotonic()
        for c, buf in list(self.clients.items()):
            if now - buf[1] > self.client_timeout:
                self._drop(c)
        return answered

    def _reply(self, c, reply):
        try:
            c.setblocking(True)
            c.settimeout(1.0)
            c.sendall(encode(reply))
        except (OSError, ProtocolError):
            pass
        self._drop(c)


# ---------------------------------------------------------------- discovery

def read_listener(folder=None):
    """GIMP's listener file, or None."""
    path = os.path.join(folder or link_dir(), LISTENER_FILE)
    try:
        info = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or "port" not in info or "token" not in info:
        return None
    return info


def write_pending(link_id, manifest, tiles=None, folder=None):
    d = os.path.join(folder or link_dir(), PENDING_DIR)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.json" % link_id)
    write_json(path, {"format": FORMAT, "v": PROTOCOL, "manifest": manifest,
                      "tiles": tiles, "time": time.time()})
    return path


def take_pending(folder=None, max_age=PENDING_MAX_AGE):
    """Returns the pending open requests (newest last) and removes their
    files; requests older than max_age are removed unopened."""
    d = os.path.join(folder or link_dir(), PENDING_DIR)
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    now = time.time()
    for n in names:
        if not n.endswith(".json"):
            continue
        p = os.path.join(d, n)
        try:
            req = read_json(p)
        except (OSError, ValueError):
            req = None
        try:
            os.unlink(p)
        except OSError:
            pass
        if not isinstance(req, dict) or req.get("format") != FORMAT:
            continue
        if now - float(req.get("time", 0)) > max_age:
            continue
        out.append(req)
    out.sort(key=lambda r: r.get("time", 0))
    return out


def load_manifest(path):
    m = read_json(path)
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise ProtocolError("not a GIMP Link manifest: %s" % path)
    if m.get("version") != PROTOCOL:
        raise ProtocolError("manifest version %r not supported" % m.get("version"))
    if not isinstance(m.get("tiles"), list) or not m["tiles"]:
        raise ProtocolError("manifest has no tiles")
    return m


def port_in_use_error(e):
    return getattr(e, "errno", None) in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1))
