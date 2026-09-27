# Headless Blender: builds the test scene, registers the add-on from the
# repo and runs Edit in GIMP on four images while GIMP is not listening
# (so pending requests are written). Checks the texture files, the UV
# files, the island masks and the manifests. Saves the .blend.
#
#   blender -b --factory-startup --python tests/blender_export.py -- WORK
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import re
import sys

import bpy
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene  # noqa: E402
import gimp_link  # noqa: E402
from gimp_link import gbl_png, gbl_protocol as proto, link  # noqa: E402

work = sys.argv[sys.argv.index("--") + 1]
os.makedirs(work, exist_ok=True)
T = scene.Results("BLENDER EXPORT")

gimp_link.register()
albedo, data, file8, normal8 = scene.build(work)
vl = bpy.context.view_layer
cube = bpy.data.objects["Cube"]

T.check("not /tmp", not proto.is_temporary_path(proto.link_dir()), proto.link_dir())
manifests = {}
for im in (albedo, data, file8, normal8):
    try:
        level, text = link.edit_in_gimp(im, vl, cube)
        T.check("edit in GIMP %s (GIMP away)" % im.name, "Start GIMP" in text, text)
        manifests[im.name] = im.gimplink.manifest
    except Exception as e:
        import traceback
        traceback.print_exc()
        T.check("edit in GIMP %s" % im.name, False, e)

M = {k: proto.load_manifest(v) for k, v in manifests.items()}
with open(os.path.join(work, "manifests.json"), "w") as f:
    json.dump(manifests, f, indent=2)

# where the files went
a = M[scene.ALBEDO]
folder = os.path.dirname(manifests[scene.ALBEDO])
T.check("exchange folder next to the .blend", folder.startswith(os.path.join(work, "scene_gimplink")), folder)
T.check("albedo exchange mode", a["mode"] == "exchange", a["mode"])
t = a["tiles"][0]
T.check("albedo texture is 16-bit PNG", t["format"] == "png" and t["bit_depth"] == 16, t)
img = gbl_png.read(t["texture"])
T.check("albedo PNG has no colour chunks", set(img[5]) == {"IHDR", "IDAT", "IEND"}, img[5])
got = np.frombuffer(img[4], dtype=">u2").reshape(scene.H, scene.W, 4)
want = scene.albedo_bytes().astype(np.uint32) * 257
T.check("albedo 16-bit values exact (8-bit k -> 257 k)", np.array_equal(got, want),
        np.abs(got.astype(int) - want).max())
T.check("albedo points at the exchange file", os.path.samefile(bpy.path.abspath(albedo.filepath), t["texture"])
        and albedo.source == "FILE", albedo.filepath)
T.check("albedo colour space kept", albedo.colorspace_settings.name == "sRGB", albedo.colorspace_settings.name)

d = M[scene.DATA]
td = d["tiles"][0]
T.check("data map is EXR", td["format"] == "exr" and td["texture"].endswith(".exr") and not td["half"], td)
T.check("data manifest says data", d["image"]["is_data"] and d["image"]["colorspace"] == "Non-Color", d["image"])
T.check("data colour space kept", data.colorspace_settings.name == "Non-Color", data.colorspace_settings.name)
fresh = bpy.data.images.load(td["texture"])
fresh.colorspace_settings.name = "Non-Color"
T.check("data EXR values exact", np.array_equal(scene.pixels(fresh), scene.data_floats()),
        np.abs(scene.pixels(fresh) - scene.data_floats()).max())
T.check("data image reads the same after re-pointing", np.array_equal(scene.pixels(data), scene.data_floats()))
bpy.data.images.remove(fresh)

f8 = M[scene.FILE8]
t8 = f8["tiles"][0]
T.check("file8 edited in place", f8["mode"] == "in-place" and os.path.samefile(t8["texture"],
        os.path.join(work, "textures", "file8.png")), f8["mode"])
T.check("file8 keeps its 8-bit depth", t8["bit_depth"] == 8, t8)
n8 = M[scene.NORMAL8]
T.check("normal8 in place, data", n8["mode"] == "in-place" and n8["image"]["is_data"], n8["image"])

# UV files
svg = open(t["uv_svg"]).read()
T.check("UV layout SVG has the 6 faces (twice: halo and line)", svg.count("M") == 12 and t["faces"] == 6,
        svg.count("M"))
T.check("UV layout SVG size is the image size", 'width="192" height="128"' in svg)
isvg = open(t["islands_svg"]).read()
ids = re.findall(r'id="(island-\d+)"', isvg)
T.check("6 islands, one path each", t["islands"] == 6 and ids == ["island-%d" % i for i in range(6)], ids)
m0 = re.search(r'id="island-0" d="([^"]+)"', isvg).group(1)
T.check("island 0 outline is its rectangle", m0.count("M") == 1 and "8.000 120.000" in m0
        and "56.000 72.000" in m0, m0)

idimg = gbl_png.read(t["island_ids"])
idv = np.frombuffer(idimg[4], dtype=">u2").reshape(scene.H, scene.W)
want_ids = np.zeros((scene.H, scene.W), dtype=np.uint16)
for i in range(6):
    x0, y0, x1, y1 = scene.island_rect(i)
    want_ids[y0:y1, x0:x1] = i + 1
T.check("island id mask exact (16-bit, id + 1)", idimg[3] == 16 and np.array_equal(idv, want_ids),
        int((idv != want_ids).sum()))
mimg = gbl_png.read(t["islands_mask"])
mv = np.frombuffer(mimg[4], dtype=np.uint8).reshape(scene.H, scene.W)
T.check("islands mask exact", np.array_equal(mv, np.where(want_ids > 0, 255, 0)))

# manifest, pending, blender section
b = a["blender"]
T.check("manifest has Blender's port and token", b["port"] == link.State.server.port and len(b["token"]) == 32, b)
T.check("manifest only readable by the user", (os.stat(manifests[scene.ALBEDO]).st_mode & 0o077) == 0)
pend = os.listdir(os.path.join(proto.link_dir(), proto.PENDING_DIR))
T.check("pending requests written", len(pend) == 4, pend)
T.check("image link saved on the image", albedo.gimplink.link_id == a["link_id"])

# a second export reuses the link and folder
lid = albedo.gimplink.link_id
link.edit_in_gimp(albedo, vl, cube)
T.check("second export keeps the link id", albedo.gimplink.link_id == lid
        and os.path.dirname(albedo.gimplink.manifest) == folder)

# UDIM in place: two tiles on disk
tex = os.path.join(work, "textures")
for n, v in ((1001, 40), (1002, 90)):
    arr = np.full((32, 32, 4), v, dtype=np.uint8)
    arr[..., 3] = 255
    gbl_png.write(os.path.join(tex, "udim.%d.png" % n), 32, 32, 4, 8, arr.tobytes())
ud = bpy.data.images.load(os.path.join(tex, "udim.1001.png"))
ud.source = "TILED"
if len(ud.tiles) < 2:
    ud.tiles.new(tile_number=1002)
ud.reload()
um = bpy.data.meshes.new("udim")
import bmesh  # noqa: E402
bm = bmesh.new()
bmesh.ops.create_grid(bm, x_segments=2, y_segments=1, size=1.0)
uvl = bm.loops.layers.uv.new("UVMap")
bm.faces.ensure_lookup_table()
for i, f in enumerate(bm.faces):
    for l, c in zip(f.loops, [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]):
        l[uvl].uv = (c[0] + i, c[1])
bm.to_mesh(um)
bm.free()
uo = bpy.data.objects.new("UdimPlane", um)
bpy.context.scene.collection.objects.link(uo)
umat = bpy.data.materials.new("udim")
if hasattr(umat, "use_nodes"):
    umat.use_nodes = True
umat.node_tree.nodes.new("ShaderNodeTexImage").image = ud
um.materials.append(umat)
try:
    link.edit_in_gimp(ud, vl, uo)
    um_ = proto.load_manifest(ud.gimplink.manifest)
    tiles = [x["number"] for x in um_["tiles"]]
    T.check("UDIM: two tiles in place", um_["udim"] and tiles == [1001, 1002] and um_["mode"] == "in-place",
            (tiles, um_["mode"]))
    T.check("UDIM: one island per tile", [x["islands"] for x in um_["tiles"]] == [1, 1],
            [x["islands"] for x in um_["tiles"]])
    T.check("UDIM: tile paths", um_["tiles"][1]["texture"].endswith("udim.1002.png"), um_["tiles"][1]["texture"])
except Exception as e:
    import traceback
    traceback.print_exc()
    T.check("UDIM export", False, e)

bpy.ops.wm.save_mainfile()
T.done()
gimp_link.unregister()
