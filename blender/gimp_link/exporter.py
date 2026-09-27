# GIMP Link for Blender: writes what GIMP needs for one image: the
# texture (in place or in the exchange folder), the UV layout, the
# island outlines and masks, and manifest.json.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import re
import time

import bpy
import numpy as np

from . import gbl_png
from . import gbl_protocol as proto
from . import islands as isl

# formats GIMP can write back into the image's own file
IN_PLACE = {".png": "png", ".exr": "exr", ".tif": "tiff", ".tiff": "tiff",
            ".tga": "tga", ".bmp": "bmp", ".webp": "webp"}
LINEAR_NAMES = ("scene_linear", "ACEScg", "ACES2065-1")


class ExportError(Exception):
    pass


def safe_name(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return s[:48] or "image"


def is_data(image):
    cs = image.colorspace_settings
    return bool(getattr(cs, "is_data", False)) or cs.name == "Non-Color"


def is_linear(image):
    n = image.colorspace_settings.name
    return n.startswith("Linear") or n in LINEAR_NAMES


def exchange_dir(image, link_id, location="BLEND"):
    """The exchange folder of an image: next to a saved .blend, else in
    the link folder. Never under /tmp or Blender's temp folder."""
    blend = bpy.data.filepath
    name = "%s-%s" % (safe_name(image.name), link_id[:6])
    if location == "BLEND" and blend:
        folder = os.path.dirname(os.path.abspath(blend))
        stem = os.path.splitext(os.path.basename(blend))[0]
        path = os.path.join(folder, stem + "_gimplink", name)
        tmp = bpy.app.tempdir.rstrip("/") if bpy.app.tempdir else None
        if not proto.is_temporary_path(path) and not (tmp and path.startswith(tmp)):
            return path
    return os.path.join(proto.link_dir(), "exchange", name)


def tile_paths(image):
    """{tile number: absolute file path} of an image backed by files."""
    path = bpy.path.abspath(image.filepath_raw or image.filepath, library=image.library)
    path = os.path.normpath(path)
    if image.source != "TILED":
        return {1001: path}
    out = {}
    for t in image.tiles:
        if "<UDIM>" in path:
            out[t.number] = path.replace("<UDIM>", str(t.number))
        else:
            out[t.number] = re.sub(r"(?<!\d)\d{4}(?=\.[^.]+$)", str(t.number), path)
    return out


def in_place_ok(image):
    if image.packed_file is not None or image.source not in ("FILE", "TILED"):
        return False
    if len(image.packed_files) > 0:
        return False
    for p in tile_paths(image).values():
        ext = os.path.splitext(p)[1].lower()
        if ext not in IN_PLACE or not os.path.isfile(p) or not os.access(p, os.W_OK):
            return False
        if proto.is_temporary_path(p):
            return False
    return True


def png_depth(path):
    try:
        with open(path, "rb") as f:
            head = f.read(26)
        if head.startswith(gbl_png.SIGNATURE):
            return head[24]
    except OSError:
        pass
    return None


def exr_is_half(path):
    """True if the first channel of an OpenEXR file is half float."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
        i = head.index(b"channels\x00chlist\x00")
        start = i + len(b"channels\x00chlist\x00") + 4
        name_end = head.index(b"\x00", start)
        ptype = int.from_bytes(head[name_end + 1:name_end + 5], "little")
        return ptype == 1
    except (OSError, ValueError):
        return False


def read_pixels(image):
    """Pixels as float32 (h, w, 4), rows from the top."""
    w, h = image.size
    px = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(px)
    return px.reshape(h, w, 4)[::-1]


def write_png16(image, path):
    """A colour byte image as 16-bit PNG, exactly (8-bit k becomes 257 k)."""
    w, h = image.size
    a = read_pixels(image)
    u16 = np.clip(np.rint(a * 65535.0), 0, 65535).astype(">u2")
    gbl_png.write(path, w, h, 4, 16, u16.tobytes())


def write_exr(image, path):
    """A float or data image as 32-bit float OpenEXR, through a
    temporary float image and Image.save (never save_render)."""
    w, h = image.size
    tmp = bpy.data.images.new("gimplink-tmp", w, h, alpha=True, float_buffer=True)
    try:
        if is_data(image):
            tmp.colorspace_settings.name = "Non-Color"
        px = np.empty(w * h * 4, dtype=np.float32)
        image.pixels.foreach_get(px)
        tmp.pixels.foreach_set(px)
        tmp.file_format = "OPEN_EXR"
        tmp.filepath_raw = path
        tmp.save(filepath=path, save_copy=True)
    finally:
        bpy.data.images.remove(tmp)


def write_png16_blender(image, path):
    """A float image with a non-linear colour space as 16-bit PNG, with
    Blender's own encoding (Image.save of a float image writes 16 bit)."""
    old = image.file_format
    try:
        image.file_format = "PNG"
        image.save(filepath=path, save_copy=True)
    finally:
        image.file_format = old


def repoint(image, path, colorspace):
    """Points an image at a file of the exchange folder, keeping its
    colour space; the packed copy, if any, is dropped."""
    if image.packed_file is not None:
        image.unpack(method="REMOVE")
    image.source = "FILE"
    rel = path
    if bpy.data.filepath:
        try:
            rel = bpy.path.relpath(path)
        except ValueError:  # another drive on Windows
            rel = path
    image.filepath = rel
    image.reload()
    if image.colorspace_settings.name != colorspace:
        image.colorspace_settings.name = colorspace


def material_uses(mat, image):
    tree = getattr(mat, "node_tree", None)
    if tree is not None:
        for n in tree.nodes:
            if n.type == "TEX_IMAGE" and n.image == image:
                return n
    return None


def uv_map_of(node):
    """The UV map named by a UV Map node feeding an Image Texture node."""
    if node is None:
        return None
    inp = node.inputs.get("Vector")
    if inp is None or not inp.is_linked:
        return None
    src = inp.links[0].from_node
    if src.type == "UVMAP" and src.uv_map:
        return src.uv_map
    return None


def users(image, view_layer, active=None):
    """[(object, material indices or None, uv map name or None)] of the
    meshes whose materials use the image; else the active mesh."""
    out = []
    for ob in view_layer.objects:
        if ob.type != "MESH":
            continue
        indices = set()
        uv_name = None
        for i, slot in enumerate(ob.material_slots):
            mat = slot.material
            if mat is None:
                continue
            node = material_uses(mat, image)
            if node is not None or any(i == image for i in getattr(mat, "texture_paint_images", [])):
                indices.add(i)
                uv_name = uv_name or uv_map_of(node)
        if indices:
            out.append((ob, indices, uv_name))
    if not out and active is not None and active.type == "MESH":
        out.append((active, None, None))
    return out


def export_uv(image, folder, view_layer, active=None, masks=True, tiles=None):
    """Writes the UV files of every tile; returns {tile: {files, islands}}."""
    w, h = image.size
    data = isl.UVData()
    udim = image.source == "TILED"
    for ob, indices, uv_name in users(image, view_layer, active):
        data.add_mesh(ob.data, uv_name, indices, udim)
    out = {}
    for t in tiles or [1001]:
        polys = data.faces.get(t, [])
        islands = data.islands.get(t, [])
        files = {
            "uv_svg": os.path.join(folder, "uv-%d.svg" % t),
            "islands_svg": os.path.join(folder, "islands-%d.svg" % t),
        }
        proto.write_atomic(files["uv_svg"], isl.layout_svg(polys, w, h).encode("utf-8"))
        proto.write_atomic(files["islands_svg"], isl.islands_svg(islands, w, h).encode("utf-8"))
        if masks:
            files["island_ids"] = os.path.join(folder, "island-ids-%d.png" % t)
            files["islands_mask"] = os.path.join(folder, "islands-mask-%d.png" % t)
            isl.write_masks(isl.rasterize(islands, w, h), files["island_ids"], files["islands_mask"])
        out[t] = {"files": files, "islands": len(islands), "faces": len(polys)}
    return out


def texture_format(path):
    ext = os.path.splitext(path)[1].lower()
    fmt = IN_PLACE.get(ext, ext.lstrip("."))
    info = {"format": fmt}
    if fmt == "png":
        info["bit_depth"] = png_depth(path) or 16
    elif fmt == "exr":
        info["half"] = exr_is_half(path)
    return info


def export(image, view_layer, blender_info, active=None, location="BLEND", masks=True,
           dilation=0, on_write=None):
    """Prepares an image for GIMP. Returns (manifest path, manifest).

    on_write(path) is called for every texture file Blender writes, so
    the watcher can ignore its own writes."""
    if image.size[0] == 0 or image.size[1] == 0:
        raise ExportError("the image has no pixels (is its file missing?)")
    props = image.gimplink
    link_id = props.link_id or proto.new_link_id()
    folder = props.folder if props.folder and os.path.isdir(props.folder) else exchange_dir(image, link_id, location)
    os.makedirs(folder, exist_ok=True)
    colorspace = image.colorspace_settings.name

    if in_place_ok(image):
        mode = "in-place"
        if image.is_dirty:
            image.save()
            for p in tile_paths(image).values():
                on_write and on_write(p)
        textures = tile_paths(image)
    else:
        mode = "exchange"
        if image.source == "TILED":
            # tiles without files of their own: Blender writes them all
            ext = ".exr" if (image.is_float or is_data(image)) else ".png"
            path = os.path.join(folder, "texture.<UDIM>" + ext)
            image.file_format = "OPEN_EXR" if ext == ".exr" else "PNG"
            if image.packed_file is not None:
                image.unpack(method="REMOVE")
            image.filepath_raw = path
            image.save()
            textures = tile_paths(image)
        else:
            if is_data(image) or (image.is_float and is_linear(image)):
                path = os.path.join(folder, "texture.exr")
                write_exr(image, path)
            elif image.is_float:
                path = os.path.join(folder, "texture.png")
                write_png16_blender(image, path)
            else:
                path = os.path.join(folder, "texture.png")
                write_png16(image, path)
            on_write and on_write(path)
            repoint(image, path, colorspace)
            textures = {1001: path}
        for p in textures.values():
            on_write and on_write(p)

    tiles = sorted(textures)
    uv = export_uv(image, folder, view_layer, active, masks, tiles)
    manifest = {
        "format": proto.FORMAT,
        "version": proto.PROTOCOL,
        "link_id": link_id,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "image": {
            "name": image.name,
            "width": image.size[0],
            "height": image.size[1],
            "colorspace": colorspace,
            "is_data": is_data(image),
            "is_float": bool(image.is_float),
        },
        "mode": mode,
        "udim": image.source == "TILED",
        "tiles": [],
        "send": {"dilation": int(dilation)},
        "blender": blender_info,
        "blend": bpy.data.filepath,
    }
    for t in tiles:
        entry = {"number": t, "texture": textures[t], "xcf": os.path.join(folder, "%d.xcf" % t),
                 "islands": uv[t]["islands"], "faces": uv[t]["faces"]}
        entry.update(texture_format(textures[t]))
        entry.update(uv[t]["files"])
        manifest["tiles"].append(entry)
    mpath = os.path.join(folder, "manifest.json")
    proto.write_json(mpath, manifest)
    props.link_id = link_id
    props.folder = folder
    props.manifest = mpath
    props.mode = mode
    props.colorspace = colorspace
    return mpath, manifest


def refresh_uv(image, view_layer, active=None, masks=True):
    """Writes the UV files again for a linked image (Update UVs)."""
    m = proto.load_manifest(image.gimplink.manifest)
    tiles = [t["number"] for t in m["tiles"]]
    uv = export_uv(image, os.path.dirname(image.gimplink.manifest), view_layer, active, masks, tiles)
    for t in m["tiles"]:
        t["islands"] = uv[t["number"]]["islands"]
        t["faces"] = uv[t["number"]]["faces"]
        t.update(uv[t["number"]]["files"])
    proto.write_json(image.gimplink.manifest, m)
    return m
