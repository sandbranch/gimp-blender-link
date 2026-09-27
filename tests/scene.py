# Test scene for headless Blender: a cube whose six faces are six UV
# islands in a 3 x 2 grid, and images with known pixels. Imported by the
# Blender test scripts (with the repo's blender/ folder on sys.path).
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys

import bmesh
import bpy
import numpy as np

W, H = 192, 128          # image size: 3 x 2 cells of 64 x 64 px
CELL = 64
MARGIN = 8               # island rectangles are 8 px inside their cells
ALBEDO = "albedo"
DATA = "data"
FILE8 = "file8"
NORMAL8 = "normal8"

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
sys.path.insert(0, os.path.join(repo, "blender"))
sys.path.insert(0, here)


def island_rect(i):
    """Pixel rectangle (x0, y0, x1, y1), exclusive ends, y from the top,
    of the island of face i."""
    c, r = i % 3, i // 3
    x0 = c * CELL + MARGIN
    x1 = (c + 1) * CELL - MARGIN
    # row r of cells counts from the bottom in UV space
    y0 = H - (r + 1) * CELL + MARGIN
    y1 = H - r * CELL - MARGIN
    return x0, y0, x1, y1


def albedo_bytes():
    """8-bit RGBA (h, w, 4) array, rows from the top."""
    y, x = np.mgrid[0:H, 0:W]
    a = np.zeros((H, W, 4), dtype=np.uint8)
    a[..., 0] = (x * 7 + y * 3) % 256
    a[..., 1] = (x + y * 5) % 256
    a[..., 2] = 200
    a[..., 3] = 255
    return a


def data_floats():
    y, x = np.mgrid[0:H, 0:W]
    a = np.zeros((H, W, 4), dtype=np.float32)
    a[..., 0] = x / W + 0.001234
    a[..., 1] = y / H * 0.5
    a[..., 2] = 0.25 + (x % 7) * 0.013
    a[..., 3] = 1.0
    return a


def file8_bytes():
    y, x = np.mgrid[0:H, 0:W]
    a = np.zeros((H, W, 4), dtype=np.uint8)
    a[..., 0] = x % 256
    a[..., 1] = y % 256
    a[..., 2] = (x * y) % 256
    a[..., 3] = 255
    return a


def set_pixels(image, rows_from_top):
    a = np.ascontiguousarray(rows_from_top[::-1], dtype=np.float32).ravel()
    image.pixels.foreach_set(a)
    image.update()


def build(work):
    """Makes the scene in the current (factory) file and saves it as
    work/scene.blend. Returns the images."""
    from gimp_link import gbl_png
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    me = bpy.data.meshes.new("cube")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)
    uv = bm.loops.layers.uv.new("UVMap")
    bm.faces.ensure_lookup_table()
    for i, f in enumerate(bm.faces):
        x0, y0, x1, y1 = island_rect(i)
        u0, u1 = x0 / W, x1 / W
        v0, v1 = 1 - y1 / H, 1 - y0 / H
        corners = [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]
        for l, c in zip(f.loops, corners):
            l[uv].uv = c
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("Cube", me)
    bpy.context.scene.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)

    albedo = bpy.data.images.new(ALBEDO, W, H, alpha=True, float_buffer=False)
    set_pixels(albedo, albedo_bytes() / 255.0)
    data = bpy.data.images.new(DATA, W, H, alpha=True, float_buffer=True)
    data.colorspace_settings.name = "Non-Color"
    set_pixels(data, data_floats())
    os.makedirs(os.path.join(work, "textures"), exist_ok=True)
    p8 = os.path.join(work, "textures", "file8.png")
    gbl_png.write(p8, W, H, 4, 8, file8_bytes().tobytes())
    file8 = bpy.data.images.load(p8)
    file8.name = FILE8
    pn = os.path.join(work, "textures", "normal8.png")
    gbl_png.write(pn, W, H, 4, 8, file8_bytes()[:, :, [1, 0, 2, 3]].copy().tobytes())
    normal8 = bpy.data.images.load(pn)
    normal8.name = NORMAL8
    normal8.colorspace_settings.name = "Non-Color"

    mat = bpy.data.materials.new("mat")
    if hasattr(mat, "use_nodes"):
        mat.use_nodes = True
    for n, im in enumerate((albedo, data, file8, normal8)):
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = im
        node.location = (-300, -300 * n)
    ob.data.materials.append(mat)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(work, "scene.blend"))
    return albedo, data, file8, normal8


class Results:
    def __init__(self, prefix):
        self.prefix = prefix
        self.failures = 0

    def check(self, name, ok, detail=""):
        if ok:
            print("PASS %s" % name, flush=True)
        else:
            self.failures += 1
            shown = "" if isinstance(detail, str) and detail == "" else ": %s" % (detail,)
            print("FAIL %s%s" % (name, shown), flush=True)
        return ok

    def done(self):
        print("%s failures: %d" % (self.prefix, self.failures), flush=True)


def srgb_encode(lin):
    lin = np.asarray(lin, dtype=np.float64)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(np.maximum(lin, 0), 1 / 2.4) - 0.055)


def srgb_decode(enc):
    enc = np.asarray(enc, dtype=np.float64)
    return np.where(enc <= 0.04045, enc / 12.92, np.power((enc + 0.055) / 1.055, 2.4))


def pixel(image, x, y_top):
    """RGBA of pixel (x, y from the top) of a Blender image."""
    w, h = image.size
    px = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(px)
    a = px.reshape(h, w, 4)
    return a[h - 1 - y_top, x]


def pixels(image):
    w, h = image.size
    px = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(px)
    return px.reshape(h, w, 4)[::-1]
