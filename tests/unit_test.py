# Plain Python tests of the shared modules: the protocol (tokens, bad
# messages, reconnect, one request per connection), pending requests,
# the file watcher's settle logic and the PNG writer and reader. No
# Blender, no GIMP, no network beyond 127.0.0.1.
#
#   python3 tests/unit_test.py
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import zlib

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(here), "blender", "gimp_link"))
import gbl_png  # noqa: E402
import gbl_protocol as proto  # noqa: E402

failures = 0


def check(name, ok, detail=""):
    global failures
    if ok:
        print("PASS %s" % name)
    else:
        failures += 1
        print("FAIL %s%s" % (name, (": %s" % (detail,)) if detail != "" else ""))


class Serve:
    """Runs a LineServer in a thread for the test."""

    def __init__(self, token="t0k3n", port=0, handlers=None, **kw):
        self.calls = []
        h = handlers or {"open": self.on_open, "boom": self.on_boom}
        self.server = proto.LineServer(token, h, "gimp", port=port, **kw)
        self.port = self.server.port
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def on_open(self, msg):
        self.calls.append(msg)
        return {"queued": True}

    def on_boom(self, msg):
        raise RuntimeError("kaputt")

    def run(self):
        while not self.stop.is_set():
            self.server.poll(0.02)

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.server.close()


def raw_exchange(port, data, read=True, timeout=3.0):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        if not read:
            return b""
        out = b""
        while not out.endswith(b"\n"):
            c = s.recv(65536)
            if not c:
                break
            out += c
        return out


# ---------------------------------------------------------------- protocol
srv = Serve()
p = srv.port
r = proto.request(p, {"cmd": "ping"})
check("ping needs no token", r == {"ok": True, "app": "gimp", "v": 1}, r)
r = proto.request(p, {"cmd": "open", "manifest": "/x"}, token="t0k3n")
check("request with the token runs", r.get("queued") is True and srv.calls[-1]["manifest"] == "/x", r)
check("wrong token refused", proto.request(p, {"cmd": "open"}, token="nope")["error"] == "bad token")
check("missing token refused", proto.request(p, {"cmd": "open"})["error"] == "bad token")
check("empty token refused", proto.request(p, {"cmd": "open"}, token="")["error"] == "bad token")
check("token of another type refused", json.loads(raw_exchange(p, b'{"v":1,"cmd":"open","token":123}\n'))["error"]
      == "bad token")
check("unknown command refused", proto.request(p, {"cmd": "rm -rf"}, token="t0k3n")["error"] == "unknown command")
check("wrong protocol version refused", proto.request(p, {"cmd": "ping", "v": 99})["error"]
      == "unsupported protocol version")
check("not JSON refused", json.loads(raw_exchange(p, b"GET / HTTP/1.1\r\n\r\n"))["error"] == "not JSON")
check("JSON that is not an object refused", json.loads(raw_exchange(p, b"[1,2]\n"))["error"] == "not a JSON object")
check("binary garbage refused", json.loads(raw_exchange(p, b"\xff\xfe\x00\n"))["ok"] is False)
big = b'{"v":1,"cmd":"ping","x":"' + b"a" * (proto.MAX_LINE + 10)
check("oversized line refused", json.loads(raw_exchange(p, big))["error"] == "message too long")
check("handler error becomes an error reply", "kaputt" in proto.request(p, {"cmd": "boom"}, token="t0k3n")["error"])
check("server survives all that", proto.request(p, {"cmd": "ping"})["ok"])
# one request per connection: a second line on the same connection is not answered
with socket.create_connection(("127.0.0.1", p), timeout=3) as s:
    s.sendall(proto.encode({"v": 1, "cmd": "ping"}) + proto.encode({"v": 1, "cmd": "ping"}))
    data = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        data += c
check("one request, one reply, then the connection closes", data.count(b"\n") == 1, data)
# a client that connects and says nothing is dropped, others are served meanwhile
silent = socket.create_connection(("127.0.0.1", p), timeout=3)
t = time.time()
check("a silent client does not block others", proto.request(p, {"cmd": "ping"})["ok"] and time.time() - t < 1)
silent.close()
# many clients at once
res = []
ths = [threading.Thread(target=lambda: res.append(proto.request(p, {"cmd": "ping"})["ok"])) for _ in range(20)]
[x.start() for x in ths]
[x.join(5) for x in ths]
check("20 clients at once", res.count(True) == 20, res.count(True))
srv.close()
# reconnect: nobody listening, then a new server on the same port
try:
    proto.request(p, {"cmd": "ping"}, timeout=1)
    check("nobody listening raises OSError", False)
except OSError:
    check("nobody listening raises OSError", True)
srv2 = Serve(token="new", port=p)
check("reconnect after the other side restarted", proto.request(p, {"cmd": "ping"})["ok"]
      and proto.request(p, {"cmd": "open"}, token="new").get("queued"))
check("old token no longer works", proto.request(p, {"cmd": "open"}, token="t0k3n")["error"] == "bad token")
srv2.close()
slow = Serve(client_timeout=0.2)
s = socket.create_connection(("127.0.0.1", slow.port), timeout=3)
s.sendall(b'{"v":1,')
time.sleep(0.6)
try:
    rest = s.recv(10)
except OSError:
    rest = b""
check("a client that stops half way is dropped", rest == b"", rest)
s.close()
slow.close()
check("tokens are random and long", len({proto.new_token() for _ in range(100)}) == 100
      and len(proto.new_token()) == 32)
check("token compare", proto.token_ok("abc", "abc") and not proto.token_ok("abc", "abd")
      and not proto.token_ok(None, "abc") and not proto.token_ok("", ""))

# ---------------------------------------------------------------- files
tmp = tempfile.mkdtemp(prefix="gbl-unit-", dir=os.environ.get("TEST_TMP"))
os.environ["GIMP_BLENDER_LINK_DIR"] = os.path.join(tmp, "link")
check("link folder from the environment", proto.link_dir() == os.path.join(tmp, "link"))
del os.environ["GIMP_BLENDER_LINK_DIR"]
check("default link folder under home, not XDG_DATA_HOME", proto.link_dir().startswith(os.path.expanduser("~"))
      and "gimp-blender-link" in proto.link_dir())
os.environ["GIMP_BLENDER_LINK_DIR"] = os.path.join(tmp, "link")
check("temporary paths recognised", proto.is_temporary_path("/tmp/x") and not proto.is_temporary_path(tmp)
      if not tmp.startswith("/tmp") else proto.is_temporary_path("/tmp/x"))
path = os.path.join(tmp, "a", "m.json")
proto.write_json(path, {"a": 1})
check("write_json: private file, no temporary left", proto.read_json(path) == {"a": 1}
      and (os.stat(path).st_mode & 0o777) == 0o600 and os.listdir(os.path.dirname(path)) == ["m.json"])
proto.write_pending("id1", "/m1.json", [1001])
proto.write_pending("id2", "/m2.json")
old = os.path.join(proto.link_dir(), "pending", "old.json")
proto.write_json(old, {"format": proto.FORMAT, "manifest": "/old", "time": time.time() - 2 * 86400})
with open(os.path.join(proto.link_dir(), "pending", "junk.json"), "w") as f:
    f.write("{nope")
got = proto.take_pending()
check("pending requests taken, old and broken ones dropped", [g["manifest"] for g in got] == ["/m1.json", "/m2.json"]
      and os.listdir(os.path.join(proto.link_dir(), "pending")) == [], got)
check("no listener file: None", proto.read_listener() is None)
proto.write_json(os.path.join(proto.link_dir(), proto.LISTENER_FILE), {"port": 5, "token": "x"})
check("listener file read", proto.read_listener()["port"] == 5)
mp = os.path.join(tmp, "manifest.json")
proto.write_json(mp, {"format": "other"})
try:
    proto.load_manifest(mp)
    check("foreign manifest refused", False)
except proto.ProtocolError:
    check("foreign manifest refused", True)

# ---------------------------------------------------------------- watcher
w = proto.FileWatch(seen=(1, 10, 5))
check("unchanged: no reload", not w.poll((1, 10, 5)))
check("changed: waits one poll", not w.poll((2, 10, 5)))
check("stable on the next poll: reload", w.poll((2, 10, 5)))
w.mark_seen((2, 10, 5))
check("then quiet", not w.poll((2, 10, 5)))
check("still being written: waits", not w.poll((3, 11, 5)) and not w.poll((4, 12, 5)))
check("stable but incomplete: waits", not w.poll((4, 12, 5), complete=False))
check("stable and complete: reload", w.poll((4, 12, 5)))
w.mark_seen((4, 12, 5))
w.announce((5, 20, 6))
check("announced by the writer: at once", w.poll((5, 20, 6)))
w.mark_seen((5, 20, 6))
check("announcement used up", w.announced is None)
w.announce((6, 1, 1))
check("announced but another stat on disk: settle as usual", not w.poll((7, 1, 1)) and w.poll((7, 1, 1)))
check("missing file: not a change", not w.poll(None) and not w.poll(None))
fp = os.path.join(tmp, "t.png")
gbl_png.write(fp, 3, 2, 4, 8, bytes(24))
check("complete PNG looks complete", proto.looks_complete(fp))
data = open(fp, "rb").read()
open(fp, "wb").write(data[:-5])
check("truncated PNG does not", not proto.looks_complete(fp))
open(fp, "wb").write(b"")
check("empty file does not", not proto.looks_complete(fp))
ep = os.path.join(tmp, "t.exr")
open(ep, "wb").write(b"\x76\x2f\x31\x01" + bytes(100))
check("EXR magic looks complete", proto.looks_complete(ep))
open(ep, "wb").write(b"JUNK" + bytes(100))
check("EXR without magic does not", not proto.looks_complete(ep))
k1 = proto.stat_key(fp)
proto.write_atomic(fp, data)
check("atomic replace changes the stat key", proto.stat_key(fp) != k1 and proto.looks_complete(fp))
check("stat key JSON round trip", proto.stat_from_json(proto.stat_to_json(k1)) == k1
      and proto.stat_from_json(["x"]) is None and proto.stat_from_json(None) is None)

# ---------------------------------------------------------------- PNG
ok = True
for ch in (1, 2, 3, 4):
    for depth in (8, 16):
        w_, h_ = 7, 5
        n = w_ * h_ * ch
        vals = [(i * 2654435761) % (256 if depth == 8 else 65536) for i in range(n)]
        samples = bytes(vals) if depth == 8 else struct.pack(">%dH" % n, *vals)
        pp = os.path.join(tmp, "p%d%d.png" % (ch, depth))
        gbl_png.write(pp, w_, h_, ch, depth, samples)
        img = gbl_png.read(pp)
        ok = ok and img[:5] == (w_, h_, ch, depth, samples) and img[5] == ["IHDR", "IDAT", "IEND"]
check("PNG round trip, 1 to 4 channels, 8 and 16 bit, exact", ok)
check("PNG sample", gbl_png.sample(gbl_png.read(os.path.join(tmp, "p416.png")), 1, 0)
      == struct.unpack(">4H", struct.pack(">4H", *[((4 + i) * 2654435761) % 65536 for i in range(4)])))


def filtered_png(path, w_, h_, rows, ftypes):
    """A PNG whose rows use the given filter types (as other writers do)."""
    bpp = 4
    out = b""
    prev = bytes(w_ * bpp)
    for row, ft in zip(rows, ftypes):
        enc = bytearray()
        for i, x in enumerate(row):
            left = row[i - bpp] if i >= bpp else 0
            up = prev[i]
            ul = prev[i - bpp] if i >= bpp else 0
            pred = [0, left, up, (left + up) >> 1, gbl_png._paeth(left, up, ul)][ft]
            enc.append((x - pred) & 0xFF)
        out += bytes([ft]) + bytes(enc)
        prev = row
    ihdr = struct.pack(">IIBBBBB", w_, h_, 8, 6, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(gbl_png.SIGNATURE + gbl_png._chunk(b"IHDR", ihdr) + gbl_png._chunk(b"IDAT", zlib.compress(out))
                + gbl_png._chunk(b"IEND", b""))


rows = [bytes((x * 37 + y * 11) % 256 for x in range(24)) for y in range(5)]
fp2 = os.path.join(tmp, "f.png")
filtered_png(fp2, 6, 5, rows, [0, 1, 2, 3, 4])
check("PNG reader undoes all five filter types", gbl_png.read(fp2)[4] == b"".join(rows))
try:
    gbl_png.encode(2, 2, 4, 8, b"123")
    check("PNG writer checks the data size", False)
except gbl_png.PNGError:
    check("PNG writer checks the data size", True)
check("native u16 to big-endian", gbl_png.to_big_endian_u16(struct.pack("=2H", 1, 258)) == b"\x00\x01\x01\x02")

print("UNIT failures: %d" % failures)
sys.exit(1 if failures else 0)
