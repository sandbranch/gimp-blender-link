# End to end, headless: this Blender and the resident listener of the
# GIMP Link plug-in in a gimp-console started by tests/run.sh, both
# Flatpaks, talking over 127.0.0.1 and the link folder.
#
# 1. Edit in GIMP while GIMP is not running: a pending request.
#    (run.sh starts GIMP when the file TEST_EXPORTED appears.)
# 2. GIMP starts, its listener takes the pending request, builds the XCF.
# 3. Edit in GIMP with GIMP listening: open over the socket; status.
# 4. Get from GIMP (send) with seam bleed: GIMP writes the texture and
#    sends reload; Blender reloads it and sees the bled pixels.
#
#   blender -b --factory-startup --python tests/e2e_blender.py -- WORK
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
import time

import bpy
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene  # noqa: E402
import gimp_link  # noqa: E402
from gimp_link import gbl_protocol as proto, link  # noqa: E402

work = sys.argv[sys.argv.index("--") + 1]
os.makedirs(work, exist_ok=True)
T = scene.Results("E2E")
gimp_link.register()
albedo, data, file8, normal8 = scene.build(work)
vl = bpy.context.view_layer
cube = bpy.data.objects["Cube"]


def pump(seconds, until=None):
    """Runs Blender's two timers by hand (background Blender has no
    event loop) until until() is true or the time is up."""
    end = time.time() + seconds
    while time.time() < end:
        link.tick_server()
        link.check()
        if until is not None and until():
            return True
        time.sleep(0.05)
    return until() if until else True


level, text = link.edit_in_gimp(file8, vl, cube)
T.check("GIMP not running: pending request", "Start GIMP" in text, text)
open(os.environ["TEST_EXPORTED"], "w").close()


def gimp_up():
    info = proto.read_listener()
    if not info:
        return False
    try:
        return proto.request(info["port"], {"cmd": "ping"}, timeout=1).get("app") == "gimp"
    except OSError:
        return False


T.check("GIMP's listener starts with GIMP", pump(180, gimp_up))
info = proto.read_listener() or {}
T.check("listener on the test port", str(info.get("port")) == os.environ.get("GIMP_BLENDER_LINK_PORT"), info)
m8 = proto.load_manifest(file8.gimplink.manifest)
T.check("pending request opened when GIMP started (XCF built)",
        pump(60, lambda: os.path.isfile(m8["tiles"][0]["xcf"])), m8["tiles"][0]["xcf"])
T.check("pending request used up", pump(5, lambda: not os.listdir(os.path.join(proto.link_dir(), "pending"))))

link.State.notices.clear()
level, text = link.edit_in_gimp(albedo, vl, cube)
T.check("Edit in GIMP with GIMP listening", level == "INFO" and "Opening" in text, text)
ma = proto.load_manifest(albedo.gimplink.manifest)
T.check("GIMP built the XCF", pump(60, lambda: os.path.isfile(ma["tiles"][0]["xcf"])))
T.check("GIMP's notice reached Blender", pump(10, lambda: any("albedo" in n[2] for n in link.State.notices)),
        link.State.notices)


def status():
    return link.gimp_request({"cmd": "status"})


st = status()
names = {s["name"]: s for s in st.get("images", [])}
T.check("status lists both linked images", len(st.get("images", [])) == 2, st)
lay = [s["layers"] for s in st.get("images", []) if s["manifest"] == albedo.gimplink.manifest]
T.check("status shows the layers", lay and lay[0] == ["UV Layout", "UV Islands", "Paint", "Texture"], lay)
T.check("status refuses a wrong token", proto.request(info["port"], {"cmd": "status"}, token="x",
                                                      timeout=2)["error"] == "bad token")
T.check("open of a missing manifest refused", link.gimp_request({"cmd": "open", "manifest": "/nope.json"})["ok"]
        is False)

# seam bleed set in Blender (the manifest), send from GIMP, reload here
ma["send"]["dilation"] = 3
proto.write_json(albedo.gimplink.manifest, ma)
x0, y0, x1, y1 = scene.island_rect(0)
edge = scene.pixel(albedo, x0, y0 + 20).copy()
before = scene.pixel(albedo, x0 - 2, y0 + 20).copy()
link.State.last_reload.clear()
link.pull_from_gimp(albedo)
T.check("Get from GIMP: GIMP sends, Blender reloads",
        pump(30, lambda: albedo.gimplink.link_id in link.State.last_reload))
after = scene.pixel(albedo, x0 - 2, y0 + 20)
T.check("the bled pixels arrived", not np.allclose(before, after) and np.allclose(after, edge, atol=2e-4),
        (before, after, edge))
T.check("colour space unchanged", albedo.colorspace_settings.name == "sRGB")
T.check("GIMP's reload message reached Blender", pump(10, lambda: link.State.messages.get(albedo.gimplink.link_id)),
        link.State.messages)

# Update UVs: face 5 gets a material without the albedo, so the albedo
# has 5 islands now; GIMP reimports the island paths
other = bpy.data.materials.new("other")
cube.data.materials.append(other)
cube.data.polygons[5].material_index = 1


def islands_in_gimp():
    for s in status().get("images", []):
        if s["manifest"] == albedo.gimplink.manifest:
            return s["islands"]
    return None


T.check("GIMP has 6 island paths before", islands_in_gimp() == 6, islands_in_gimp())
link.update_uvs(albedo, vl, cube)
T.check("Update UVs: GIMP reimports the islands (5 now)", pump(20, lambda: islands_in_gimp() == 5),
        islands_in_gimp())
T.check("Update UVs: the SVG has 5 islands", open(ma["tiles"][0]["islands_svg"]).read().count('id="island-') == 5)

open(os.environ["TEST_DONE"], "w").close()
T.done()
gimp_link.unregister()
