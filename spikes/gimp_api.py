# Spike: GIMP 3.2 API pieces the link needs, run in gimp-console with
# python-fu-eval. SPIKE_OUT is the output folder.
import gi, os, time, struct, zlib, array
gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gimp, Gio, Gegl, GLib

out = os.environ["SPIKE_OUT"]
os.makedirs(out, exist_ok=True)
pdb = Gimp.get_pdb()


def args(name):
    p = pdb.lookup_procedure(name)
    if not p:
        print("NOPROC", name)
        return
    print("PROC", name, [(a.get_name(), a.value_type.name, getattr(a, "default_value", None)) for a in p.get_arguments()])


for n in ("file-png-export", "file-exr-export", "file-png-load", "file-svg-load", "gimp-image-merge-down",
          "gimp-drawable-merge-filter", "gimp-selection-grow"):
    args(n)

print("HAS", {k: hasattr(Gimp, k) for k in ("LinkLayer", "Display", "DrawableFilter", "Selection", "Path")})
print("Display methods", [m for m in dir(Gimp.Display) if not m.startswith("_")][:60])
print("Image methods sel", [m for m in dir(Gimp.Image) if "path" in m or "precision" in m or "merge" in m or "profile" in m])
print("PlugIn methods", [m for m in dir(Gimp.PlugIn) if "persist" in m or "temp" in m])
print("Procedure methods", [m for m in dir(Gimp.Procedure) if "persist" in m or "menu" in m or "sensitiv" in m])

# SVG with two paths and a hole
svg = os.path.join(out, "islands.svg")
with open(svg, "w") as f:
    f.write('<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">\n'
            '<path id="island-0" d="M10 10 L100 10 L100 100 L10 100 Z M40 40 L70 40 L70 70 L40 70 Z" fill="none" stroke="#000"/>\n'
            '<path id="island-1" d="M150 150 L240 150 L240 240 L150 240 Z" fill="none" stroke="#000"/>\n</svg>\n')
img = Gimp.Image.new_with_precision(256, 256, Gimp.ImageBaseType.RGB, Gimp.Precision.U16_NON_LINEAR)
bg = Gimp.Layer.new(img, "base", 256, 256, Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)
img.insert_layer(bg, None, 0)
ok, paths = img.import_paths_from_file(Gio.File.new_for_path(svg), False, False)
print("PATHS", ok, [(p.get_name(), p.get_strokes()) for p in paths])
Gimp.Selection.none(img)
for p in paths:
    img.select_item(Gimp.ChannelOps.ADD, p)
sel = img.get_selection()
print("SEL values hole(55,55)", sel.get_pixel(55, 55).get_rgba(), "in(20,20)", sel.get_pixel(20, 20).get_rgba(),
      "isl1(200,200)", sel.get_pixel(200, 200).get_rgba(), "out(120,120)", sel.get_pixel(120, 120).get_rgba())
ch = Gimp.Selection.save(img)
print("saved channel", ch.get_name() if ch else ch)

# Gegl buffer raw get / set
buf = bg.get_buffer()
rect = Gegl.Rectangle.new(0, 0, 2, 1)
data = bytes(struct.pack("<8H", 1, 2, 3, 65535, 100, 200, 300, 65535))
try:
    buf.set(rect, "R'G'B'A u16", data)
    buf.flush()
    got = buf.get(rect, 1.0, "R'G'B'A u16", Gegl.AbyssPolicy.NONE)
    print("GEGL get", type(got), struct.unpack("<8H", got))
except Exception as e:
    print("GEGL FAIL", repr(e))
bg.update(0, 0, 256, 256)

# linear u16 image: what does PNG export write?
lin = Gimp.Image.new_with_precision(4, 4, Gimp.ImageBaseType.RGB, Gimp.Precision.U16_LINEAR)
ll = Gimp.Layer.new(lin, "l", 4, 4, Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)
lin.insert_layer(ll, None, 0)
lb = ll.get_buffer()
lb.set(Gegl.Rectangle.new(0, 0, 4, 4), "RGBA u16", struct.pack("<4H", 12345, 32768, 50000, 65535) * 16)
lb.flush()
ll.update(0, 0, 4, 4)


def export_png(image, path, fmt):
    proc = pdb.lookup_procedure("file-png-export")
    cfg = proc.create_config()
    cfg.set_property("image", image)
    cfg.set_property("file", Gio.File.new_for_path(path))
    for k, v in (("format", fmt), ("include-color-profile", False), ("gamma", False), ("bkgd", False),
                 ("offs", False), ("phys", False), ("time", False), ("save-transparent", True), ("compression", 6)):
        try:
            cfg.set_property(k, v)
        except Exception as e:
            print("prop", k, e)
    r = proc.run(cfg)
    return r.index(0)


def read_png16(path):
    with open(path, "rb") as f:
        d = f.read()
    pos = 8
    idat = b""
    ihdr = None
    chunks = []
    while pos < len(d):
        n, t = struct.unpack(">I4s", d[pos:pos + 8])
        body = d[pos + 8:pos + 8 + n]
        chunks.append(t.decode())
        if t == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        if t == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    return ihdr, chunks, raw[:20]


p = os.path.join(out, "lin16.png")
print("export lin16", export_png(lin, p, "rgba16"))
print("lin16 png", read_png16(p))
t0 = time.time()
# exr export defaults
pe = os.path.join(out, "lin.exr")
print("exr", Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, lin, Gio.File.new_for_path(pe), None))
back = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(pe))
print("exr back", back.get_precision().value_nick, back.get_layers()[0].get_buffer().get(
    Gegl.Rectangle.new(0, 0, 1, 1), 1.0, "RGBA float", Gegl.AbyssPolicy.NONE).hex())

# merge timing at 2048
big = Gimp.Image.new_with_precision(2048, 2048, Gimp.ImageBaseType.RGB, Gimp.Precision.U16_NON_LINEAR)
b0 = Gimp.Layer.new(big, "b", 2048, 2048, Gimp.ImageType.RGBA_IMAGE, 100, Gimp.LayerMode.NORMAL)
big.insert_layer(b0, None, 0)
b0.fill(Gimp.FillType.TRANSPARENT)
big.select_rectangle(Gimp.ChannelOps.REPLACE, 500, 500, 400, 400)
Gimp.context_set_foreground(Gegl.Color.new("rgb(1,0,0)"))
b0.edit_fill(Gimp.FillType.FOREGROUND)
Gimp.Selection.none(big)
t0 = time.time()
top = b0
for (dx, dy) in ((1, 0), (-1, 0), (0, 1), (0, -1)):
    cp = top.copy()
    big.insert_layer(cp, None, big.get_item_position(top) + 1)
    cp.set_offsets(dx, dy)
    top = big.merge_down(top, Gimp.MergeType.CLIP_TO_IMAGE)
print("4 merges 2048 s", round(time.time() - t0, 3), top.get_offsets(), top.get_width(),
      top.get_pixel(499, 700).get_rgba(), top.get_pixel(498, 700).get_rgba())

# link layer to svg, replace svg, see update
lk = Gimp.LinkLayer.new(img, Gio.File.new_for_path(svg))
img.insert_layer(lk, None, 0)
print("link", lk.get_width(), lk.get_height(), lk.get_pixel(10, 50).get_rgba(), lk.get_pixel(200, 120).get_rgba())
svg2 = os.path.join(out, "islands.tmp")
with open(svg2, "w") as f:
    f.write('<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">\n'
            '<path d="M200 0 L200 256" fill="none" stroke="#f00" stroke-width="4"/>\n</svg>\n')
os.replace(svg2, svg)
ctx = GLib.MainContext.default()
for i in range(10):
    t = time.time()
    while time.time() - t < 0.5:
        ctx.iteration(False)
        time.sleep(0.02)
    px = lk.get_pixel(200, 120).get_rgba()
    print("after replace", i, px, lk.get_pixel(10, 50).get_rgba())
    if px[3] > 0:
        break
Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, img, Gio.File.new_for_path(os.path.join(out, "t.xcf")), None)
print("SPIKE DONE")
