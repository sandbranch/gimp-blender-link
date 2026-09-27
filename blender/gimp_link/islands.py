# GIMP Link for Blender: UV faces, islands, island outlines and masks
# from a mesh, written as SVG and PNG for GIMP.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import math

import bmesh
import numpy as np

from . import gbl_png

EPS = 1e-6


def tile_of(u, v):
    """UDIM tile number of a UV point."""
    return 1001 + int(math.floor(u)) + 10 * int(math.floor(v))


def _find(parent, i):
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _same(a, b):
    return abs(a[0] - b[0]) <= EPS and abs(a[1] - b[1]) <= EPS


class UVData:
    """UV faces of some meshes, split into UDIM tiles and islands.

    faces[tile]: list of polygons, each a list of (u, v) in tile
    coordinates (0..1); islands[tile]: list of dicts with 'loops' (the
    boundary loops, lists of (u, v)) and 'tris' (triangles, each three
    (u, v))."""

    def __init__(self):
        self.faces = {}
        self.islands = {}

    def tiles(self):
        return sorted(self.faces)

    def add_mesh(self, mesh, uv_name=None, material_indices=None, udim=False):
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            layer = bm.loops.layers.uv.get(uv_name) if uv_name else None
            if layer is None:
                layer = bm.loops.layers.uv.active
            if layer is None:
                return 0
            self._add_bmesh(bm, layer, material_indices, udim)
            return len(bm.faces)
        finally:
            bm.free()

    def _add_bmesh(self, bm, layer, material_indices, udim):
        bm.faces.ensure_lookup_table()
        faces = [f for f in bm.faces
                 if material_indices is None or f.material_index in material_indices]
        if not faces:
            return
        index = {f.index: n for n, f in enumerate(faces)}
        uv = {}
        for f in faces:
            for l in f.loops:
                uv[l.index] = tuple(l[layer].uv)
        # tile of each face, from its UV centre
        tile = {}
        for f in faces:
            if udim:
                cu = sum(uv[l.index][0] for l in f.loops) / len(f.loops)
                cv = sum(uv[l.index][1] for l in f.loops) / len(f.loops)
                tile[f.index] = tile_of(cu, cv)
            else:
                tile[f.index] = 1001

        def offset(t):
            n = t - 1001
            return (n % 10, n // 10)

        def connected(l):
            """Is the face of loop l joined in UV space to the face on
            the other side of its edge? Returns that loop or None."""
            e = l.edge
            if len(e.link_loops) != 2:
                return None
            o = e.link_loops[0] if e.link_loops[1] == l else e.link_loops[1]
            if o.face.index not in index or tile[o.face.index] != tile[l.face.index]:
                return None
            # l goes a -> b; the other loop runs b -> a or a -> b
            a, b = l, l.link_loop_next
            if o.vert == a.vert:
                oa, ob = o, o.link_loop_next
            else:
                oa, ob = o.link_loop_next, o
            if oa.vert != a.vert or ob.vert != b.vert:
                return None
            if _same(uv[a.index], uv[oa.index]) and _same(uv[b.index], uv[ob.index]):
                return o
            return None

        parent = list(range(len(faces)))
        for f in faces:
            for l in f.loops:
                o = connected(l)
                if o is not None:
                    ra, rb = _find(parent, index[f.index]), _find(parent, index[o.face.index])
                    if ra != rb:
                        parent[ra] = rb
        groups = {}
        for f in faces:
            groups.setdefault(_find(parent, index[f.index]), []).append(f)
        # stable order: by the lowest face index
        for root in sorted(groups, key=lambda r: min(f.index for f in groups[r])):
            members = groups[root]
            t = tile[members[0].index]
            ou, ov = offset(t)
            loc = lambda p: (p[0] - ou, p[1] - ov)
            polys = self.faces.setdefault(t, [])
            segments = []
            tris = []
            for f in members:
                pts = [loc(uv[l.index]) for l in f.loops]
                polys.append(pts)
                for i in range(1, len(pts) - 1):
                    tris.append((pts[0], pts[i], pts[i + 1]))
                for l in f.loops:
                    if connected(l) is None:
                        segments.append((loc(uv[l.index]), loc(uv[l.link_loop_next.index])))
            self.islands.setdefault(t, []).append({"loops": chain(segments), "tris": tris})


def _key(p):
    return (round(p[0] / EPS), round(p[1] / EPS))


def chain(segments):
    """Joins directed boundary segments into closed loops."""
    starts = {}
    for i, (a, b) in enumerate(segments):
        starts.setdefault(_key(a), []).append(i)
    used = [False] * len(segments)
    loops = []
    for i in range(len(segments)):
        if used[i]:
            continue
        used[i] = True
        a, b = segments[i]
        loop = [a]
        start = _key(a)
        cur = b
        for _ in range(len(segments)):
            if _key(cur) == start:
                break
            nxt = None
            for j in starts.get(_key(cur), ()):
                if not used[j]:
                    nxt = j
                    break
            if nxt is None:
                break
            used[nxt] = True
            loop.append(cur)
            cur = segments[nxt][1]
        loops.append(loop)
    return loops


# ---------------------------------------------------------------- output

def _xy(p, w, h):
    return "%.3f %.3f" % (p[0] * w, (1.0 - p[1]) * h)


def _d(polys, w, h, close=True):
    parts = []
    for pts in polys:
        if len(pts) < 2:
            continue
        parts.append("M" + " L".join(_xy(p, w, h) for p in pts) + (" Z" if close else ""))
    return " ".join(parts)


def _svg(w, h, body):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">\n'
            '%s</svg>\n' % (w, h, w, h, body))


def layout_svg(polys, w, h):
    """All UV faces as outlines: a light halo under a dark line, so it
    shows on dark and light textures."""
    d = _d(polys, w, h)
    body = ""
    if d:
        body = ('<path d="%s" fill="none" stroke="#ffffff" stroke-opacity="0.6" stroke-width="2" '
                'stroke-linejoin="round"/>\n'
                '<path d="%s" fill="none" stroke="#000000" stroke-width="1" stroke-linejoin="round"/>\n'
                % (d, d))
    return _svg(w, h, body)


def islands_svg(islands, w, h):
    """One closed path per island (its boundary loops), id island-N."""
    body = ""
    for n, isl in enumerate(islands):
        d = _d(isl["loops"], w, h)
        if d:
            body += ('<path id="island-%d" d="%s" fill="none" stroke="#ff00ff" stroke-width="1"/>\n'
                     % (n, d))
    return _svg(w, h, body)


def rasterize(islands, w, h):
    """Island ids at pixel centres: uint16 array (rows from the top),
    island number + 1 inside an island, 0 outside."""
    ids = np.zeros((h, w), dtype=np.uint16)
    for n, isl in enumerate(islands):
        value = n + 1
        for tri in isl["tris"]:
            xs = [p[0] * w for p in tri]
            ys = [(1.0 - p[1]) * h for p in tri]
            # pixel i covers [i, i+1), its centre is i + 0.5
            x0 = max(0, int(math.floor(min(xs) - 0.5)))
            x1 = min(w - 1, int(math.ceil(max(xs) - 0.5)))
            y0 = max(0, int(math.floor(min(ys) - 0.5)))
            y1 = min(h - 1, int(math.ceil(max(ys) - 0.5)))
            if x1 < x0 or y1 < y0:
                continue
            px, py = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
            (ax, bx, cx), (ay, by, cy) = xs, ys
            area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            if abs(area) < 1e-12:
                continue
            e0 = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
            e1 = (cx - bx) * (py - by) - (cy - by) * (px - bx)
            e2 = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
            if area > 0:
                inside = (e0 >= -1e-9) & (e1 >= -1e-9) & (e2 >= -1e-9)
            else:
                inside = (e0 <= 1e-9) & (e1 <= 1e-9) & (e2 <= 1e-9)
            region = ids[y0:y1 + 1, x0:x1 + 1]
            region[inside] = value
    return ids


def write_masks(ids, id_path, mask_path):
    h, w = ids.shape
    gbl_png.write(id_path, w, h, 1, 16, ids.astype(">u2").tobytes())
    mask = np.where(ids > 0, 255, 0).astype(np.uint8)
    gbl_png.write(mask_path, w, h, 1, 8, mask.tobytes())
