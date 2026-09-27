# GIMP Link for Blender: an exact PNG writer and reader in pure Python
# (zlib and array only), shared by the Blender add-on and the GIMP
# plug-in. It writes the numbers it is given, with no colour chunks and
# no conversion, which is what data maps need. The copies in
# blender/gimp_link/ and gimp/gimp-blender-link/ must stay identical.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import array
import os
import struct
import sys
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
COLOR_TYPES = {1: 0, 2: 4, 3: 2, 4: 6}  # channels -> PNG colour type
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PNGError(Exception):
    pass


def _chunk(kind, body):
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def to_big_endian_u16(data):
    """Native-order 16-bit samples (e.g. from Gegl) to big-endian bytes."""
    a = array.array("H")
    a.frombytes(bytes(data))
    if sys.byteorder == "little":
        a.byteswap()
    return a.tobytes()


def encode(width, height, channels, depth, samples, level=6):
    """PNG bytes. samples: rows from the top, big-endian for 16-bit."""
    if channels not in COLOR_TYPES or depth not in (8, 16):
        raise PNGError("unsupported channels %r or depth %r" % (channels, depth))
    stride = width * channels * depth // 8
    if len(samples) != stride * height:
        raise PNGError("expected %d bytes, got %d" % (stride * height, len(samples)))
    mv = memoryview(samples)
    raw = b"".join(b"\x00" + mv[y * stride:(y + 1) * stride].tobytes() for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, depth, COLOR_TYPES[channels], 0, 0, 0)
    return (SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, level))
            + _chunk(b"IEND", b""))


def write(path, width, height, channels, depth, samples, level=6, atomic=True):
    """Writes a PNG; with atomic, through a temporary file in the same
    folder and os.replace."""
    data = encode(width, height, channels, depth, samples, level)
    if not atomic:
        with open(path, "wb") as f:
            f.write(data)
        return
    folder = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(folder, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(data, height, stride, bpp):
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ftype = data[pos]
        line = bytearray(data[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up_left = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], up_left)) & 0xFF
        elif ftype != 0:
            raise PNGError("bad filter type %d" % ftype)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return bytes(out)


def read(path):
    """Returns (width, height, channels, depth, samples, chunk names).
    samples: rows from the top, 16-bit samples big-endian. Palette,
    interlaced and sub-byte images are not supported (the link never
    writes them; GIMP and Blender write 8 and 16 bit)."""
    with open(path, "rb") as f:
        d = f.read()
    if not d.startswith(SIGNATURE):
        raise PNGError("not a PNG: %s" % path)
    pos = 8
    ihdr = None
    idat = []
    names = []
    while pos + 8 <= len(d):
        n, kind = struct.unpack(">I4s", d[pos:pos + 8])
        body = d[pos + 8:pos + 8 + n]
        names.append(kind.decode("latin-1"))
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + n
    if ihdr is None:
        raise PNGError("no IHDR")
    width, height, depth, ctype, _, _, interlace = ihdr
    if ctype not in CHANNELS or ctype == 3 or depth not in (8, 16) or interlace:
        raise PNGError("unsupported PNG (type %d, depth %d, interlace %d)" % (ctype, depth, interlace))
    channels = CHANNELS[ctype]
    bpp = channels * depth // 8
    stride = width * bpp
    raw = zlib.decompress(b"".join(idat))
    return width, height, channels, depth, _unfilter(raw, height, stride, bpp), names


def sample(img, x, y):
    """The samples of pixel (x, y) (y from the top) of a read() result,
    as integers."""
    width, height, channels, depth, samples = img[:5]
    if depth == 8:
        i = (y * width + x) * channels
        return tuple(samples[i:i + channels])
    i = (y * width + x) * channels * 2
    return struct.unpack(">%dH" % channels, samples[i:i + channels * 2])
