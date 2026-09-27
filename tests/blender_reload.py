# Headless Blender, after the GIMP test: opens the saved .blend, checks
# the pixels GIMP painted at their UV locations and the colour spaces,
# then the watcher (settle check, own writes, a half-written file, a
# conflict with unsaved paint) and the socket (reload message, token).
#
#   blender -b --factory-startup --python tests/blender_reload.py -- WORK
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
import threading
import time

import bpy
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene  # noqa: E402
import gimp_link  # noqa: E402
from gimp_link import gbl_png, gbl_protocol as proto, link  # noqa: E402

work = sys.argv[sys.argv.index("--") + 1]
T = scene.Results("BLENDER RELOAD")
gimp_link.register()
bpy.ops.wm.open_mainfile(filepath=os.path.join(work, "scene.blend"))
link.start()

im = {n: bpy.data.images[n] for n in (scene.ALBEDO, scene.DATA, scene.FILE8, scene.NORMAL8)}
ONE16 = 1.0 / 65535

# what GIMP painted, at the UV location of the island it went into
x0, y0, x1, y1 = scene.island_rect(2)
u, v = (x0 + 5.5) / scene.W, 1 - (y0 + 5.5) / scene.H
px, py = int(u * scene.W), int((1 - v) * scene.H)
a = scene.pixel(im[scene.ALBEDO], px, py)
enc = scene.srgb_encode(a[:3])
want = np.array((12345, 40000, 54321)) / 65535
T.check("albedo: GIMP's paint at its UV location within 1/65535", np.all(np.abs(enc - want) <= ONE16),
        (enc * 65535, want * 65535))
T.check("albedo: 16-bit PNG loads as float", im[scene.ALBEDO].is_float)
T.check("albedo: colour space unchanged", im[scene.ALBEDO].colorspace_settings.name == "sRGB",
        im[scene.ALBEDO].colorspace_settings.name)
x0, y0, x1, y1 = scene.island_rect(4)
d = scene.pixel(im[scene.DATA], x0 + 3, y0 + 3)
T.check("data: GIMP's paint exact (EXR)", np.array_equal(d, np.array((0.123456789, 0.5, 0.987654321, 1.0),
        dtype=np.float32)), d)
T.check("data: colour space unchanged", im[scene.DATA].colorspace_settings.name == "Non-Color",
        im[scene.DATA].colorspace_settings.name)
T.check("data: pixels outside the paint exact",
        np.array_equal(scene.pixels(im[scene.DATA])[20, 50], scene.data_floats()[20, 50]))
x0, y0, x1, y1 = scene.island_rect(1)
f = scene.pixel(im[scene.FILE8], x0 + 3, y0 + 3)
T.check("file8: GIMP's paint exact (8-bit in place)", np.allclose(f, np.array((255, 0, 128, 255)) / 255,
        atol=1e-7, rtol=0), f * 255)
T.check("file8: seam bleed reached Blender", np.allclose(scene.pixel(im[scene.FILE8], x0 - 2, y0 + 10),
        np.array((255, 0, 128, 255)) / 255, atol=1e-7, rtol=0))
n = scene.pixel(im[scene.NORMAL8], x0 + 1, y0 + 1)
T.check("normal8: data numbers exact", np.allclose(n, np.array((128, 128, 255, 255)) / 255, atol=1e-7, rtol=0), n * 255)
T.check("normal8: colour space unchanged", im[scene.NORMAL8].colorspace_settings.name == "Non-Color")

# refreshed manifests carry this Blender's port and token
m = proto.load_manifest(im[scene.ALBEDO].gimplink.manifest)
T.check("manifest refreshed with this session's port", m["blender"]["port"] == link.State.server.port
        and m["blender"]["token"] == link.State.token, m["blender"])

# the watcher
albedo = im[scene.ALBEDO]
tex = m["tiles"][0]["texture"]


def write16(value, path=tex, atomic=True):
    arr = np.zeros((scene.H, scene.W, 4), dtype=">u2")
    arr[...] = value
    gbl_png.write(path, scene.W, scene.H, 4, 16, arr.tobytes(), atomic=atomic)


def val(image):
    return scene.srgb_encode(scene.pixel(image, 10, 10)[:3]) * 65535


write16((20000, 30000, 40000, 65535))
first = link.check()
second = link.check()
T.check("watcher waits one poll (settle), then reloads", first == [] and second == [albedo], (first, second))
T.check("reloaded pixels", np.all(np.abs(val(albedo) - (20000, 30000, 40000)) <= 1), val(albedo))
T.check("colour space unchanged after reload", albedo.colorspace_settings.name == "sRGB")
T.check("nothing more to reload", link.check() == [] and link.check() == [])

write16((1000, 1000, 1000, 65535))
link.mark_written(tex)
T.check("Blender's own writes are not reloaded", link.check() == [] and link.check() == []
        and np.all(np.abs(val(albedo) - (20000, 30000, 40000)) <= 1))

arr5 = np.full((scene.H, scene.W, 4), 5000, dtype=">u2")
arr5[..., 3] = 65535
full = gbl_png.encode(scene.W, scene.H, 4, 16, arr5.tobytes())
with open(tex, "wb") as fh:
    fh.write(full[:len(full) // 2])
T.check("a half-written PNG is not reloaded", all(link.check() == [] for _ in range(4)))
with open(tex, "wb") as fh:
    fh.write(full)
c1, c2 = link.check(), link.check()
T.check("the finished file is then reloaded", c1 + c2 == [albedo] and np.all(np.abs(val(albedo) - 5000) <= 1),
        val(albedo))

# unsaved paint in Blender and a change on disk: a conflict, not a reload
buf = scene.pixels(albedo).copy()
buf[0:4, 0:4] = (1.0, 0.0, 0.0, 1.0)
scene.set_pixels(albedo, buf)
T.check("image has unsaved changes", albedo.is_dirty)
write16((30000, 30000, 30000, 65535))
link.check()
link.check()
T.check("change on disk with unsaved paint: conflict, no reload", albedo.gimplink.conflict and albedo.is_dirty)

# GIMP's reload message over the socket reloads anyway, keeping a backup
port, token = link.State.server.port, link.State.token
replies = {}


def client():
    replies["ping"] = proto.request(port, {"cmd": "ping"})
    replies["bad"] = proto.request(port, {"cmd": "reload", "link_id": albedo.gimplink.link_id}, token="wrong")
    replies["reload"] = proto.request(port, {"cmd": "reload", "link_id": albedo.gimplink.link_id, "tile": 1001,
                                             "path": tex, "stat": proto.stat_to_json(proto.stat_key(tex))},
                                      token=token)
    replies["nolink"] = proto.request(port, {"cmd": "reload", "link_id": "nope"}, token=token)


th = threading.Thread(target=client)
th.start()
end = time.time() + 10
while th.is_alive() and time.time() < end:
    link.tick_server()
    time.sleep(0.01)
th.join(1)
T.check("ping answered", replies.get("ping", {}).get("app") == "blender", replies.get("ping"))
T.check("wrong token refused", replies.get("bad", {}).get("error") == "bad token", replies.get("bad"))
T.check("reload queued", replies.get("reload", {}).get("queued") is True, replies.get("reload"))
T.check("unknown link refused", replies.get("nolink", {}).get("ok") is False)
link.check()
T.check("reload message reloads despite unsaved paint", not albedo.is_dirty and not albedo.gimplink.conflict
        and np.all(np.abs(val(albedo) - 30000) <= 1), val(albedo))
backups = os.listdir(os.path.join(albedo.gimplink.folder, "backups"))
T.check("unsaved Blender pixels kept in backups/", len(backups) == 1, backups)
if backups:
    bimg = gbl_png.read(os.path.join(albedo.gimplink.folder, "backups", backups[0]))
    T.check("backup has Blender's red corner", gbl_png.sample(bimg, 1, 1)[:3] == (65535, 0, 0),
            gbl_png.sample(bimg, 1, 1))

# unregistering removes the timers
gimp_link.unregister()
T.check("timers gone after unregister", not bpy.app.timers.is_registered(link.tick_server)
        and not bpy.app.timers.is_registered(link.tick_watch) and link.State.server is None)
T.done()
