# Headless GIMP (gimp-console, python-fu-eval): builds the XCFs from the
# manifests the Blender export test wrote, checks layers, precision,
# pixels, paths and the islands channel, paints known rectangles, sends
# the textures back (one with seam bleed) and checks the files. Also
# the UV link layer following its SVG, reopening an XCF, a change made
# outside GIMP, and the reload message to a stand-in for Blender.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import struct
import sys
import threading
import time

here = os.environ["TEST_HERE"]
work = os.environ["TEST_WORK"]
sys.path.insert(0, os.path.join(os.path.dirname(here), "gimp", "gimp-blender-link"))

import gi  # noqa: E402
gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gimp, Gio, Gegl, GLib  # noqa: E402

import gbl_gimp  # noqa: E402
import gbl_png  # noqa: E402
import gbl_protocol as proto  # noqa: E402

W, H, CELL, MARGIN = 192, 128, 64, 8
failures = 0


def check(name, ok, detail=""):
    global failures
    if ok:
        print("PASS %s" % name, flush=True)
    else:
        failures += 1
        print("FAIL %s%s" % (name, (": " + str(detail)) if detail else ""), flush=True)
    return ok


def island_rect(i):
    c, r = i % 3, i // 3
    return (c * CELL + MARGIN, H - (r + 1) * CELL + MARGIN, (c + 1) * CELL - MARGIN, H - r * CELL - MARGIN)


def raw(layer, fmt, x, y, w=1, h=1):
    return layer.get_buffer().get(Gegl.Rectangle.new(x, y, w, h), 1.0, fmt, Gegl.AbyssPolicy.NONE)


def put(layer, fmt, x, y, w, h, pixel_bytes):
    b = layer.get_buffer()
    b.set(Gegl.Rectangle.new(x, y, w, h), fmt, pixel_bytes * (w * h))
    b.flush()
    layer.update(x, y, w, h)


def layer_named(image, name):
    for l in image.get_layers():
        if l.get_name() == name:
            return l
    return None


def pump(seconds):
    ctx = GLib.MainContext.default()
    end = time.time() + seconds
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.02)


manifests = json.load(open(os.path.join(work, "manifests.json")))

# a stand-in for Blender's listener, to receive the reload message
got = []
fake = proto.LineServer("fake-token", {"reload": lambda m: got.append(m) or {"queued": True}}, "blender")
stop = threading.Event()
threading.Thread(target=lambda: [fake.poll(0.05) for _ in iter(stop.is_set, True)], daemon=True).start()
m = proto.load_manifest(manifests["albedo"])
m["blender"] = {"port": fake.port, "token": "fake-token", "pid": 0, "version": "test"}
proto.write_json(manifests["albedo"], m)

# ---------------------------------------------------------------- albedo
t0 = time.time()
img = gbl_gimp.build(manifests["albedo"], 1001)
check("albedo XCF built", os.path.isfile(m["tiles"][0]["xcf"]), time.time() - t0)
names = [l.get_name() for l in img.get_layers()]
check("albedo layers top to bottom", names == ["UV Layout", "UV Islands", "Paint", "Texture"], names)
uvl = layer_named(img, "UV Layout")
check("UV Layout is a link layer to the SVG", uvl.is_link_layer() and
      uvl.get_file().get_path() == m["tiles"][0]["uv_svg"], uvl.get_file().get_path())
check("helpers marked", gbl_gimp.is_helper(uvl) and gbl_gimp.is_helper(layer_named(img, "UV Islands"))
      and not gbl_gimp.is_helper(layer_named(img, "Paint")))
check("albedo precision 16-bit perceptual", img.get_precision() == Gimp.Precision.U16_NON_LINEAR,
      img.get_precision().value_nick)
tex = layer_named(img, "Texture")
png = gbl_png.read(m["tiles"][0]["texture"])
mism = 0
for (x, y) in ((0, 0), (17, 33), (191, 127), (100, 64), (5, 120)):
    want = gbl_png.sample(png, x, y)
    have = struct.unpack("<4H", raw(tex, "R'G'B'A u16", x, y))
    mism += want != have
check("albedo Texture layer has the file's numbers", mism == 0, mism)
paths = gbl_gimp.island_paths(img)
check("6 island paths", [p.get_name() for p in paths] == ["Island %d" % i for i in range(6)],
      [p.get_name() for p in paths])
ch = [c for c in img.get_channels() if c.get_name() == "UV Islands"]
mask = gbl_png.read(m["tiles"][0]["islands_mask"])
if check("islands channel", len(ch) == 1):
    cb = ch[0].get_buffer().get(Gegl.Rectangle.new(0, 0, W, H), 1.0, "Y u8", Gegl.AbyssPolicy.NONE)
    check("islands channel equals Blender's islands mask", bytes(cb) == mask[4],
          sum(a != b for a, b in zip(bytes(cb), mask[4])))
check("Paint is the active layer", [l.get_name() for l in img.get_selected_layers()] == ["Paint"])
check("albedo XCF saved clean", not img.is_dirty())

# paint a known rectangle into island 2 and send
paint = layer_named(img, "Paint")
x0, y0, x1, y1 = island_rect(2)
PAINT16 = (12345, 40000, 54321, 65535)
put(paint, "R'G'B'A u16", x0 + 4, y0 + 4, 20, 10, struct.pack("<4H", *PAINT16))
r = gbl_gimp.send(img, dilation=0)
out = gbl_png.read(m["tiles"][0]["texture"])
check("albedo sent as 16-bit RGBA", out[2:4] == (4, 16), out[2:4])
check("painted rectangle in the texture", gbl_png.sample(out, x0 + 5, y0 + 5) == PAINT16,
      gbl_png.sample(out, x0 + 5, y0 + 5))
check("unpainted pixels unchanged", gbl_png.sample(out, 3, 3) == gbl_png.sample(png, 3, 3))
check("UV layout not in the texture", gbl_png.sample(out, x0, y0 + 20) == gbl_png.sample(png, x0, y0 + 20))
check("no temporary files left", not [f for f in os.listdir(os.path.dirname(r["texture"])) if f.endswith(".tmp")])
t1 = time.time()
while not got and time.time() - t1 < 5:
    time.sleep(0.05)
check("reload message reached Blender's stand-in", r["blender"] and got and got[-1]["link_id"] == m["link_id"]
      and proto.stat_from_json(got[-1]["stat"]) == proto.stat_key(r["texture"]), got)
check("XCF saved on send", not img.is_dirty())

# the UV link layer follows its SVG
svg = m["tiles"][0]["uv_svg"]
before = struct.unpack("<4H", raw(uvl, "R'G'B'A u16", 150, 2))
with open(svg + ".new", "w") as f:
    f.write('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="192" height="128" '
            'viewBox="0 0 192 128"><rect x="140" y="0" width="20" height="10" fill="#ff0000"/></svg>')
os.replace(svg + ".new", svg)
after = before
for _ in range(50):
    pump(0.1)
    after = struct.unpack("<4H", raw(uvl, "R'G'B'A u16", 150, 2))
    if after != before:
        break
check("UV link layer updates when the SVG changes", before[3] == 0 and after == (65535, 0, 0, 65535),
      (before, after))

# reopen: close the image and open the manifest again
xcf = m["tiles"][0]["xcf"]
img.delete()
img2, how = gbl_gimp.open_tile(manifests["albedo"], 1001, display=False)
check("reopen loads the XCF", how == "loaded", how)
p2 = layer_named(img2, "Paint")
check("reopened XCF keeps the paint layer", p2 is not None and
      struct.unpack("<4H", raw(p2, "R'G'B'A u16", x0 + 5, y0 + 5)) == PAINT16)
check("reopened XCF keeps the link", gbl_gimp.read_link(img2)["link_id"] == m["link_id"])
# a change outside GIMP (same pixels, new file): comes in as a layer
data = open(m["tiles"][0]["texture"], "rb").read()
proto.write_atomic(m["tiles"][0]["texture"], data)
img3, how = gbl_gimp.open_tile(manifests["albedo"], 1001, display=False)
check("change outside GIMP detected", img3 == img2 and how == "open+changed", how)
check("change outside GIMP becomes a new layer on top of the painting",
      img3.get_layers()[2].get_name().startswith("Changed outside GIMP"), [l.get_name() for l in img3.get_layers()])
_, how = gbl_gimp.open_tile(manifests["albedo"], 1001, display=False)
check("no second change layer when nothing changed", how == "open", how)

# ---------------------------------------------------------------- data (EXR)
md = proto.load_manifest(manifests["data"])
dimg = gbl_gimp.build(manifests["data"], 1001)
check("data precision float linear", dimg.get_precision() == Gimp.Precision.FLOAT_LINEAR,
      dimg.get_precision().value_nick)
dtex = layer_named(dimg, "Texture")
x, y = 50, 20
want = (x / W + 0.001234, y / H * 0.5, 0.25 + (x % 7) * 0.013, 1.0)
have = struct.unpack("<4f", raw(dtex, "RGBA float", x, y))
check("data Texture layer exact floats", have == struct.unpack("<4f", struct.pack("<4f", *want)), (have, want))
DPAINT = (0.123456789, 0.5, 0.987654321, 1.0)
x0, y0, x1, y1 = island_rect(4)
put(layer_named(dimg, "Paint"), "RGBA float", x0 + 2, y0 + 2, 8, 8, struct.pack("<4f", *DPAINT))
gbl_gimp.send(dimg, dilation=0, notify=False)
back = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(md["tiles"][0]["texture"]))
bl = back.get_layers()[0]
check("data EXR written as float", back.get_precision() == Gimp.Precision.FLOAT_LINEAR, back.get_precision().value_nick)
check("data EXR painted values exact", struct.unpack("<4f", raw(bl, "RGBA float", x0 + 3, y0 + 3))
      == struct.unpack("<4f", struct.pack("<4f", *DPAINT)))
check("data EXR other values exact", struct.unpack("<4f", raw(bl, "RGBA float", x, y)) == have)
back.delete()

# ---------------------------------------------------------------- normal8 (data PNG in place)
mn = proto.load_manifest(manifests["normal8"])
nimg = gbl_gimp.build(manifests["normal8"], 1001)
check("data PNG in GIMP as 16-bit linear", nimg.get_precision() == Gimp.Precision.U16_LINEAR,
      nimg.get_precision().value_nick)
ntex = layer_named(nimg, "Texture")
orig = gbl_png.read(mn["tiles"][0]["texture"])
x, y = 77, 51
check("data PNG numbers kept (no conversion)", struct.unpack("<4H", raw(ntex, "RGBA u16", x, y))
      == tuple(257 * v for v in gbl_png.sample(orig, x, y)))
x0, y0, x1, y1 = island_rect(1)
put(layer_named(nimg, "Paint"), "RGBA u16", x0, y0, 10, 10, struct.pack("<4H", 128 * 257, 128 * 257, 255 * 257, 65535))
# half transparent paint over a data map: the blend happens on the numbers
put(layer_named(nimg, "Paint"), "RGBA u16", x0 + 20, y0, 1, 1, struct.pack("<4H", 65535, 65535, 65535, 32768))
gbl_gimp.send(nimg, dilation=0, notify=False)
nout = gbl_png.read(mn["tiles"][0]["texture"])
check("data PNG written back 8-bit", nout[3] == 8, nout[3])
check("data PNG painted numbers exact", gbl_png.sample(nout, x0 + 1, y0 + 1) == (128, 128, 255, 255),
      gbl_png.sample(nout, x0 + 1, y0 + 1))
under = gbl_png.sample(orig, x0 + 20, y0)
wantb = tuple(round((u / 255 + (1 - u / 255) * 32768 / 65535) * 255) for u in under[:3])
check("data PNG blends linearly on the stored numbers",
      all(abs(a - b) <= 1 for a, b in zip(gbl_png.sample(nout, x0 + 20, y0)[:3], wantb)),
      (gbl_png.sample(nout, x0 + 20, y0), wantb))
check("data PNG unpainted numbers kept", gbl_png.sample(nout, 3, 3) == gbl_png.sample(orig, 3, 3))

# ---------------------------------------------------------------- file8 with seam bleed
m8 = proto.load_manifest(manifests["file8"])
f8 = gbl_gimp.build(manifests["file8"], 1001)
orig8 = gbl_png.read(m8["tiles"][0]["texture"])
check("8-bit file worked on at 16 bit", f8.get_precision() == Gimp.Precision.U16_NON_LINEAR)
P = (255, 0, 128, 255)
x0, y0, x1, y1 = island_rect(1)
put(layer_named(f8, "Paint"), "R'G'B'A u16", x0, y0, x1 - x0, y1 - y0, struct.pack("<4H", *[v * 257 for v in P]))
N = 4
t0 = time.time()
r8 = gbl_gimp.send(f8, dilation=N, notify=False)
dt = time.time() - t0
o8 = gbl_png.read(m8["tiles"][0]["texture"])
check("file8 written back as 8-bit", o8[3] == 8 and r8["dilation"] == N, (o8[3], r8["dilation"]))
rects = [island_rect(i) for i in range(6)]


def cheb(x, y, r):
    rx0, ry0, rx1, ry1 = r
    dx = max(rx0 - x, 0, x - (rx1 - 1))
    dy = max(ry0 - y, 0, y - (ry1 - 1))
    return max(dx, dy)


bad_ring = bad_far = bad_in = bad_other = 0
for yy in range(H):
    for xx in range(W):
        v = gbl_png.sample(o8, xx, yy)
        d = [cheb(xx, yy, r) for r in rects]
        if d[1] == 0:
            bad_in += v != P
        elif min(d) == 0:
            bad_in += v != gbl_png.sample(orig8, xx, yy)
        elif d[1] <= N:
            bad_ring += v != P
        elif min(d) > N:
            bad_far += v != gbl_png.sample(orig8, xx, yy)
        else:
            # the ring of another island: a colour of that island within N px
            i = d.index(min(d))
            rx0, ry0, rx1, ry1 = rects[i]
            ok = False
            for sy in range(max(ry0, yy - N), min(ry1, yy + N + 1)):
                for sx in range(max(rx0, xx - N), min(rx1, xx + N + 1)):
                    if gbl_png.sample(orig8, sx, sy) == v:
                        ok = True
                        break
                if ok:
                    break
            bad_other += not ok
check("seam bleed: inside islands unchanged or painted", bad_in == 0, bad_in)
check("seam bleed: %d px ring around the painted island has its colour" % N, bad_ring == 0, bad_ring)
check("seam bleed: rings of other islands take their own colours", bad_other == 0, bad_other)
check("seam bleed: farther than %d px unchanged" % N, bad_far == 0, bad_far)
check("seam bleed steps cover every distance", all(
    set(range(n + 1)) <= {sum(s for k, s in enumerate(gbl_gimp.dilation_steps(n)) if mask >> k & 1)
                          for mask in range(1 << len(gbl_gimp.dilation_steps(n)))}
    for n in range(0, 65)))
print("INFO seam bleed send took %.2f s" % dt)

# ---------------------------------------------------------------- bad input
try:
    gbl_gimp.send(Gimp.Image.new(8, 8, Gimp.ImageBaseType.RGB))
    check("send refuses an unlinked image", False)
except gbl_gimp.LinkError:
    check("send refuses an unlinked image", True)

stop.set()
fake.close()
for leftover in Gimp.get_images():
    leftover.delete()
print("GIMP BUILD SEND failures: %d" % failures, flush=True)
