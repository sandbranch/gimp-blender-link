# GIMP Link for Blender: the GIMP side's work, without Gimp.main, so the
# tests can run it in gimp-console: build the layered XCF from a
# manifest, reopen it, send the texture back, dilate islands.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import time

import gi
gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gimp, Gio, Gegl  # noqa: E402

import gbl_png  # noqa: E402
import gbl_protocol as proto  # noqa: E402

PARASITE = "gimp-blender-link"
HELPER = "gimp-blender-link-helper"
ISLAND_PATH = "gimp-blender-link-island"
CHUNK_ROWS = 256

NAMES = {
    "texture": "Texture",
    "paint": "Paint",
    "uv": "UV Layout",
    "islands": "UV Islands",
    "channel": "UV Islands",
    "changed": "Changed outside GIMP %s",
}


class LinkError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _parasite(name, obj):
    data = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
    return Gimp.Parasite.new(name, Gimp.PARASITE_PERSISTENT, list(data))


def read_link(image):
    p = image.get_parasite(PARASITE)
    if p is None:
        return None
    try:
        return json.loads(bytes(p.get_data()).decode("utf-8"))
    except ValueError:
        return None


def write_link(image, info):
    image.attach_parasite(_parasite(PARASITE, info))


def is_helper(item):
    return item.get_parasite(HELPER) is not None


def mark_helper(item):
    item.attach_parasite(_parasite(HELPER, b"1"))


def tile_entry(manifest, number):
    for t in manifest["tiles"]:
        if int(t["number"]) == int(number):
            return t
    raise LinkError("tile %s is not in the manifest" % number)


def precision_for(tile, is_data):
    if tile.get("format") == "exr":
        return Gimp.Precision.HALF_LINEAR if tile.get("half") else Gimp.Precision.FLOAT_LINEAR
    return Gimp.Precision.U16_LINEAR if is_data else Gimp.Precision.U16_NON_LINEAR


def raw_format(precision):
    """The babl format whose numbers are the image's stored numbers."""
    return {
        Gimp.Precision.U16_NON_LINEAR: "R'G'B'A u16",
        Gimp.Precision.U16_LINEAR: "RGBA u16",
        Gimp.Precision.FLOAT_LINEAR: "RGBA float",
        Gimp.Precision.HALF_LINEAR: "RGBA half",
    }[precision]


def source_format(precision, is_data):
    """How to read a loaded texture so that its numbers are kept: data
    maps in a PNG are read as the perceptual numbers they are stored as
    and written as linear ones, without conversion."""
    if precision == Gimp.Precision.U16_LINEAR and is_data:
        return "R'G'B'A u16"
    return raw_format(precision)


def copy_pixels(src, dst, src_fmt, dst_fmt):
    """Copies the numbers of src into dst, CHUNK_ROWS rows at a time."""
    w, h = dst.get_width(), dst.get_height()
    sb, db = src.get_buffer(), dst.get_buffer()
    for y in range(0, h, CHUNK_ROWS):
        n = min(CHUNK_ROWS, h - y)
        r = Gegl.Rectangle.new(0, y, w, n)
        db.set(r, dst_fmt, sb.get(r, 1.0, src_fmt, Gegl.AbyssPolicy.NONE))
    db.flush()
    dst.update(0, 0, w, h)


def load_single(path):
    """Loads an image file as one layer with alpha: (image, layer)."""
    im = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path))
    if im is None:
        raise LinkError("GIMP could not load %s" % path)
    layers = im.get_layers()
    layer = layers[0] if len(layers) == 1 else im.merge_visible_layers(Gimp.MergeType.CLIP_TO_IMAGE)
    if not layer.has_alpha():
        layer.add_alpha()
    return im, layer


def texture_layer(image, path, name, is_data):
    """A new layer with the numbers of the texture file."""
    src, sl = load_single(path)
    try:
        w, h = image.get_width(), image.get_height()
        if (sl.get_width(), sl.get_height()) != (w, h):
            raise LinkError("%s is %dx%d, the image is %dx%d"
                            % (path, sl.get_width(), sl.get_height(), w, h))
        prec = image.get_precision()
        layer = Gimp.Layer.new(image, name, w, h, Gimp.ImageType.RGBA_IMAGE, 100,
                               Gimp.LayerMode.NORMAL)
        copy_pixels(sl, layer, source_format(prec, is_data), raw_format(prec))
        return layer
    finally:
        src.delete()


def find_open(xcf):
    xcf = os.path.abspath(xcf)
    for im in Gimp.get_images():
        f = im.get_xcf_file() or im.get_file()
        if f is not None and f.get_path() and os.path.abspath(f.get_path()) == xcf:
            return im
    return None


def linked_images():
    out = []
    for im in Gimp.get_images():
        info = read_link(im)
        if info:
            out.append((im, info))
    return out


def image_for(manifest_path, tile=None):
    for im, info in linked_images():
        if os.path.abspath(info.get("manifest", "")) == os.path.abspath(manifest_path):
            if tile is None or int(info.get("tile", 1001)) == int(tile):
                return im
    return None


def save_xcf(image, path):
    if not Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, Gio.File.new_for_path(path), None):
        raise LinkError("could not save %s" % path)
    image.clean_all()


# ---------------------------------------------------------------- islands

def import_islands(image, tile):
    """Island paths and the islands channel; old ones are replaced."""
    for p in image.get_paths():
        if p.get_parasite(ISLAND_PATH) is not None:
            image.remove_path(p)
    for c in image.get_channels():
        if is_helper(c):
            image.remove_channel(c)
    path = tile.get("islands_svg")
    if not path or not os.path.isfile(path) or not tile.get("islands"):
        return []
    ok, paths = image.import_paths_from_file(Gio.File.new_for_path(path), False, False)
    if not ok or not paths:
        return []
    for n, p in enumerate(paths):
        p.set_name("Island %d" % n)
        p.attach_parasite(_parasite(ISLAND_PATH, b"%d" % n))
    Gimp.Selection.none(image)
    for p in paths:
        image.select_item(Gimp.ChannelOps.ADD, p)
    ch = Gimp.Selection.save(image)
    if ch is not None:
        ch.set_name(NAMES["channel"])
        ch.set_visible(False)
        mark_helper(ch)
    Gimp.Selection.none(image)
    return paths


def island_paths(image):
    return [p for p in image.get_paths() if p.get_parasite(ISLAND_PATH) is not None]


# ---------------------------------------------------------------- build

def link_layer(image, path, name, opacity, visible, position):
    layer = Gimp.LinkLayer.new(image, Gio.File.new_for_path(path))
    if layer is None:
        raise LinkError("could not make a link layer of %s" % path)
    layer.set_name(name)
    image.insert_layer(layer, None, position)
    layer.set_opacity(opacity)
    layer.set_visible(visible)
    layer.set_lock_content(True)
    mark_helper(layer)
    return layer


def build(manifest_path, number):
    """Builds and saves the XCF of one tile. Returns the image."""
    m = proto.load_manifest(manifest_path)
    t = tile_entry(m, number)
    info = m["image"]
    is_data = bool(info.get("is_data"))
    w, h = int(info["width"]), int(info["height"])
    prec = precision_for(t, is_data)
    image = Gimp.Image.new_with_precision(w, h, Gimp.ImageBaseType.RGB, prec)
    image.undo_disable()
    base = texture_layer(image, t["texture"], NAMES["texture"], is_data)
    image.insert_layer(base, None, 0)
    paint = Gimp.Layer.new(image, NAMES["paint"], w, h, Gimp.ImageType.RGBA_IMAGE, 100,
                           Gimp.LayerMode.NORMAL)
    image.insert_layer(paint, None, 0)
    paint.fill(Gimp.FillType.TRANSPARENT)
    if t.get("islands_svg") and os.path.isfile(t["islands_svg"]):
        link_layer(image, t["islands_svg"], NAMES["islands"], 100.0, False, 0)
    if t.get("uv_svg") and os.path.isfile(t["uv_svg"]):
        link_layer(image, t["uv_svg"], NAMES["uv"], 60.0, True, 0)
    import_islands(image, t)
    write_link(image, {"manifest": os.path.abspath(manifest_path), "tile": int(number),
                       "link_id": m["link_id"], "stamp": proto.stat_to_json(proto.stat_key(t["texture"])),
                       "options": {}})
    image.set_selected_layers([paint])
    image.undo_enable()
    save_xcf(image, t["xcf"])
    return image


def add_external_change(image, t, is_data):
    """The texture changed outside GIMP since the XCF was last in sync:
    it comes in as a new layer on top of the painting layers."""
    layer = texture_layer(image, t["texture"], NAMES["changed"] % time.strftime("%Y-%m-%d %H:%M"),
                          is_data)
    pos = 0
    for n, l in enumerate(image.get_layers()):
        if not is_helper(l):
            pos = n
            break
    image.insert_layer(layer, None, pos)
    return layer


def open_tile(manifest_path, number, display=True):
    """Shows the XCF of a tile: the open image, the saved XCF, or a new
    build. Returns (image, what happened)."""
    m = proto.load_manifest(manifest_path)
    t = tile_entry(m, number)
    image = find_open(t["xcf"]) or image_for(manifest_path, number)
    how = "open"
    if image is None and os.path.isfile(t["xcf"]):
        image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(t["xcf"]))
        how = "loaded"
        if display:
            _display(image)
    elif image is None:
        image = build(manifest_path, number)
        if display:
            _display(image)
        return image, "built"
    info = read_link(image) or {}
    info["manifest"] = os.path.abspath(manifest_path)
    now = proto.stat_key(t["texture"])
    if proto.stat_from_json(info.get("stamp")) != now:
        add_external_change(image, t, bool(m["image"].get("is_data")))
        info["stamp"] = proto.stat_to_json(now)
        how += "+changed"
    write_link(image, info)
    Gimp.displays_flush()
    return image, how


def _display(image):
    try:
        Gimp.Display.new(image)
        Gimp.displays_flush()
    except Exception:  # gimp-console has no displays
        pass


# ---------------------------------------------------------------- send

def dilation_steps(n):
    """Shift sizes whose subset sums give every distance 0..n."""
    steps = []
    s = 1
    while n > 0:
        step = min(s, n)
        steps.append(step)
        n -= step
        s *= 2
    return steps


def dilate(image, layer, n, paths):
    """Spreads the colours of the islands n pixels outward (Chebyshev
    distance) over what is outside them; inside the islands and farther
    than n pixels nothing changes. Returns the resulting layer."""
    if n <= 0 or not paths:
        return layer
    Gimp.Selection.none(image)
    for p in paths:
        image.select_item(Gimp.ChannelOps.ADD, p)
    if Gimp.Selection.is_empty(image):
        return layer
    b = layer.copy()
    image.insert_layer(b, None, image.get_item_position(layer))
    Gimp.Selection.invert(image)
    b.edit_clear()
    Gimp.Selection.none(image)
    for s in dilation_steps(n):
        for dx, dy in ((s, 0), (-s, 0), (0, s), (0, -s)):
            c = b.copy()
            image.insert_layer(c, None, image.get_item_position(b) + 1)
            c.set_offsets(dx, dy)
            b = image.merge_down(b, Gimp.MergeType.CLIP_TO_IMAGE)
    return image.merge_down(b, Gimp.MergeType.CLIP_TO_IMAGE)


def flattened(image):
    """A copy of the image with only the merged painting (helpers and
    hidden layers dropped): (copy, layer)."""
    dup = image.duplicate()
    dup.undo_disable()
    for l in dup.get_layers():
        if is_helper(l) or not l.get_visible():
            dup.remove_layer(l)
    layers = dup.get_layers()
    if not layers:
        raise LinkError("nothing visible to send")
    merged = layers[0] if len(layers) == 1 else dup.merge_visible_layers(Gimp.MergeType.CLIP_TO_IMAGE)
    if (merged.get_width(), merged.get_height()) != (dup.get_width(), dup.get_height()) \
            or merged.get_offsets()[1:] != (0, 0):
        merged.resize_to_image_size()
    if not merged.has_alpha():
        merged.add_alpha()
    return dup, merged


def write_texture(dup, layer, t, is_data):
    path = t["texture"]
    fmt = t.get("format", "png")
    w, h = dup.get_width(), dup.get_height()
    if fmt == "png":
        depth = int(t.get("bit_depth") or 16)
        babl = ("RGBA" if is_data else "R'G'B'A") + (" u16" if depth == 16 else " u8")
        buf = layer.get_buffer()
        rows = []
        for y in range(0, h, CHUNK_ROWS):
            n = min(CHUNK_ROWS, h - y)
            data = buf.get(Gegl.Rectangle.new(0, y, w, n), 1.0, babl, Gegl.AbyssPolicy.NONE)
            rows.append(gbl_png.to_big_endian_u16(data) if depth == 16 else bytes(data))
        gbl_png.write(path, w, h, 4, depth, b"".join(rows))
        return
    folder = os.path.dirname(path)
    base, ext = os.path.splitext(os.path.basename(path))
    tmp = os.path.join(folder, ".%s.%d.tmp%s" % (base, os.getpid(), ext))
    try:
        if not Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, dup, Gio.File.new_for_path(tmp), None):
            raise LinkError("GIMP could not write %s" % tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def notify_blender(m, msg, timeout=2.0):
    b = m.get("blender") or {}
    if not b.get("port") or not b.get("token"):
        return None
    try:
        return proto.request(b["port"], msg, token=b["token"], timeout=timeout)
    except (OSError, proto.ProtocolError) as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def send(image, dilation=-1, save=True, notify=True):
    """Writes the image's texture for Blender and tells Blender.
    Returns a dict with what was done."""
    info = read_link(image)
    if not info:
        raise LinkError("this image is not linked to Blender")
    m = proto.load_manifest(info["manifest"])
    t = tile_entry(m, info.get("tile", 1001))
    is_data = bool(m["image"].get("is_data"))
    if dilation is None or dilation < 0:
        dilation = int((info.get("options") or {}).get("dilation", -1))
        if dilation < 0:
            dilation = int((m.get("send") or {}).get("dilation", 0))
    dup, layer = flattened(image)
    try:
        layer = dilate(dup, layer, dilation, island_paths(dup))
        for l in dup.get_layers():
            if l != layer:
                dup.remove_layer(l)
        write_texture(dup, layer, t, is_data)
    finally:
        dup.delete()
    stamp = proto.stat_key(t["texture"])
    info["stamp"] = proto.stat_to_json(stamp)
    write_link(image, info)
    reply = None
    if notify:
        # first Blender, then the XCF: saving a big XCF takes a while
        reply = notify_blender(m, {"cmd": "reload", "link_id": m["link_id"], "tile": int(t["number"]),
                                   "path": t["texture"], "stat": proto.stat_to_json(stamp)})
    if save and (info.get("options") or {}).get("save_xcf", True):
        save_xcf(image, t["xcf"])
    return {"texture": t["texture"], "dilation": dilation, "stamp": stamp,
            "blender": bool(reply and reply.get("ok")), "reply": reply}


def uv_changed(manifest_path):
    """Reimports the island paths of the open images of a manifest (the
    link layers follow their SVG files by themselves)."""
    m = proto.load_manifest(manifest_path)
    n = 0
    for im, info in linked_images():
        if os.path.abspath(info.get("manifest", "")) != os.path.abspath(manifest_path):
            continue
        import_islands(im, tile_entry(m, info.get("tile", 1001)))
        n += 1
    Gimp.displays_flush()
    return n
