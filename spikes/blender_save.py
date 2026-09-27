# Spike: what Image.save() writes from headless Blender 5.2, and what
# a filepath change plus reload does to the pixels and the colour space.
# Run: flatpak run org.blender.Blender -b --factory-startup --python spikes/blender_save.py -- OUTDIR
import bpy, sys, os, struct, zlib

out = sys.argv[sys.argv.index("--") + 1]
os.makedirs(out, exist_ok=True)


def png_info(p):
    with open(p, "rb") as f:
        d = f.read(33)
    w, h, depth, ctype = struct.unpack(">IIBB", d[16:26])
    return dict(w=w, h=h, depth=depth, ctype=ctype)


def show(tag, img):
    px = img.pixels[:4]
    print(tag, "float", img.is_float, "depth", img.depth, "cs", img.colorspace_settings.name,
          "fmt", img.file_format, "src", img.source, "path", img.filepath, "px", [round(v, 5) for v in px])


def mk(name, float_buffer, value, cs=None):
    img = bpy.data.images.new(name, 4, 4, alpha=True, float_buffer=float_buffer)
    if cs:
        img.colorspace_settings.name = cs
    img.pixels[:] = list(value) * 16
    return img


# 1: byte image, sRGB, PNG
b = mk("byte", False, (0.2, 0.4, 0.8, 1.0))
show("byte before", b)
b.file_format = "PNG"
p = os.path.join(out, "byte.png")
try:
    b.save(filepath=p)
    print("byte saved", os.path.exists(p), png_info(p))
except Exception as e:
    print("byte save FAIL", e)
show("byte after save", b)

# 2: float image, sRGB, PNG
f = mk("float", True, (0.2, 0.4, 0.8, 1.0))
show("float before", f)
f.file_format = "PNG"
p = os.path.join(out, "float.png")
try:
    f.save(filepath=p)
    print("float png saved", png_info(p))
except Exception as e:
    print("float png FAIL", e)
show("float after save", f)

# 3: float EXR, Non-Color
d = mk("data", True, (0.123456789, 0.5, 0.987654321, 1.0), "Non-Color")
d.file_format = "OPEN_EXR"
p = os.path.join(out, "data.exr")
try:
    d.save(filepath=p)
    print("exr saved", os.path.getsize(p))
except Exception as e:
    print("exr FAIL", e)
show("data after save", d)

# 4: re-point the byte image at the float PNG and reload
b.filepath = os.path.join(out, "float.png")
b.source = "FILE"
b.reload()
show("byte repointed to float.png", b)

# 5: re-point data at its exr and reload, colour space kept?
d2 = mk("data2", True, (0.0, 0.0, 0.0, 1.0), "Non-Color")
d2.filepath = os.path.join(out, "data.exr")
d2.source = "FILE"
d2.reload()
show("data2 repointed to exr", d2)
d3 = bpy.data.images.load(os.path.join(out, "data.exr"))
show("data.exr fresh load", d3)

# 6: packed image, unpack REMOVE then repoint
pk = mk("packed", False, (1.0, 0.0, 0.0, 1.0))
pk.pack()
print("packed?", pk.packed_file is not None, pk.source)
pk.unpack(method="REMOVE")
print("after unpack", pk.packed_file, pk.source, pk.filepath)
pk.filepath = os.path.join(out, "byte.png")
pk.source = "FILE"
pk.reload()
show("packed repointed", pk)

# 7: 16-bit PNG: load exactness of an arbitrary value
# write a 16-bit PNG by hand: value 12345/65535 in R
def write_png16(path, w, h, rgba16):
    raw = b"".join(b"\x00" + b"".join(struct.pack(">HHHH", *rgba16) for _ in range(w)) for _ in range(h))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 6, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

write_png16(os.path.join(out, "hand16.png"), 4, 4, (12345, 40000, 65535, 65535))
h = bpy.data.images.load(os.path.join(out, "hand16.png"))
show("hand16 sRGB", h)
h.colorspace_settings.name = "Non-Color"
show("hand16 Non-Color (after set)", h)
h.reload()
show("hand16 Non-Color reloaded", h)
print("expect noncolor", 12345 / 65535, 40000 / 65535)
print("image.save params", [p.identifier for p in bpy.types.Image.bl_rna.functions["save"].parameters])
print("colorspaces", [i.identifier for i in bpy.types.ColorManagedInputColorspaceSettings.bl_rna.properties['name'].enum_items])
