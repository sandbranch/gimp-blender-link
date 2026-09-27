# GIMP Link for Blender: the running link in Blender. A socket for
# GIMP's messages and a file watcher, both serviced by bpy.app.timers on
# the main thread (no threads: Blender's API is not thread safe), and a
# client for requests to GIMP.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shlex
import subprocess
import time

import bpy

from . import exporter
from . import gbl_protocol as proto

APP = "blender"
SERVER_INTERVAL = 0.1


class NotListening(Exception):
    pass


class State:
    """Runtime state (not saved in the .blend)."""
    server = None
    token = None
    watches = {}        # (link_id, tile) -> FileWatch
    explicit = set()    # (link_id, tile) announced by GIMP's reload
    textures = {}       # link_id -> {tile: path}, from the manifest
    notices = []        # [(time, level, text)] from GIMP
    last_reload = {}    # link_id -> time
    messages = {}       # link_id -> number of reload messages from GIMP
    interval = 0.5


def prefs():
    """The add-on preferences, or defaults when the add-on was
    registered by hand (as the tests do)."""
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        class Defaults:
            exchange_location = "BLEND"
            poll_interval = 0.5
            dilation = 0
            island_masks = True
            gimp_command = ""
        return Defaults()


def blender_info():
    return {"port": State.server.port if State.server else 0, "token": State.token or "",
            "pid": os.getpid(), "version": bpy.app.version_string}


# ---------------------------------------------------------------- images

def linked_images():
    return [im for im in bpy.data.images if im.gimplink.link_id and im.gimplink.manifest]


def image_by_link(link_id):
    for im in bpy.data.images:
        if im.gimplink.link_id == link_id:
            return im
    return None


def textures_of(image):
    """{tile: path} of a linked image, from its manifest (cached)."""
    lid = image.gimplink.link_id
    t = State.textures.get(lid)
    if t is None:
        try:
            m = proto.load_manifest(image.gimplink.manifest)
            t = {int(x["number"]): x["texture"] for x in m["tiles"]}
        except (OSError, ValueError, proto.ProtocolError, KeyError):
            t = {}
        State.textures[lid] = t
    return t


def watch(link_id, tile):
    key = (link_id, tile)
    w = State.watches.get(key)
    if w is None:
        w = State.watches[key] = proto.FileWatch()
    return w


def mark_written(path):
    """Called for files Blender writes itself: not a change to reload."""
    key = proto.stat_key(path)
    for im in linked_images():
        for tile, p in textures_of(im).items():
            if os.path.abspath(p) == os.path.abspath(path):
                watch(im.gimplink.link_id, tile).mark_seen(key)
    _pending_writes[os.path.abspath(path)] = key


_pending_writes = {}  # writes before the image had its link id


def mark_all_seen():
    """After loading a .blend: Blender has just read every file."""
    State.textures.clear()
    for im in linked_images():
        for tile, p in textures_of(im).items():
            watch(im.gimplink.link_id, tile).mark_seen(proto.stat_key(p))


def redraw():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in ("IMAGE_EDITOR", "VIEW_3D"):
                area.tag_redraw()


def backup(image):
    """Keeps Blender's unsaved pixels before a reload replaces them."""
    folder = os.path.join(image.gimplink.folder or exporter.exchange_dir(image, image.gimplink.link_id),
                          "backups")
    os.makedirs(folder, exist_ok=True)
    ext = ".exr" if image.file_format == "OPEN_EXR" else ".png"
    path = os.path.join(folder, "%s-%s%s" % (exporter.safe_name(image.name),
                                              time.strftime("%Y%m%d-%H%M%S"), ext))
    if image.source == "TILED":
        path = path[:-len(ext)] + ".<UDIM>" + ext
    image.save(filepath=path, save_copy=True)
    return path


def reload_image(image, keep_backup=True):
    """Reloads a linked image from its files. Returns the backup path if
    unsaved Blender pixels were kept."""
    kept = None
    if image.is_dirty and keep_backup:
        try:
            kept = backup(image)
        except (RuntimeError, OSError) as e:
            print("GIMP Link: could not keep a backup of %s: %s" % (image.name, e))
    cs = image.gimplink.colorspace or image.colorspace_settings.name
    image.reload()
    if image.colorspace_settings.name != cs:
        image.colorspace_settings.name = cs
    image.update_tag()
    image.gimplink.conflict = False
    lid = image.gimplink.link_id
    for tile, p in textures_of(image).items():
        watch(lid, tile).mark_seen(proto.stat_key(p))
        State.explicit.discard((lid, tile))
    State.last_reload[lid] = time.time()
    redraw()
    return kept


def check(now_explicit_only=False):
    """One pass of the watcher. Returns the images reloaded."""
    done = []
    for im in linked_images():
        lid = im.gimplink.link_id
        changed = False
        explicit = False
        for tile, p in textures_of(im).items():
            w = watch(lid, tile)
            if w.seen is None and p in _pending_writes:
                w.mark_seen(_pending_writes.pop(p))
            key = proto.stat_key(p)
            if w.seen is None:
                w.mark_seen(key)
                continue
            if w.poll(key, proto.looks_complete(p)):
                changed = True
                explicit = explicit or (lid, tile) in State.explicit
        if not changed:
            continue
        if im.is_dirty and not explicit:
            # somebody else wrote the file while Blender has unsaved
            # paint: do not throw that paint away without asking
            if not im.gimplink.conflict:
                im.gimplink.conflict = True
                redraw()
            continue
        reload_image(im)
        done.append(im)
    return done


# ---------------------------------------------------------------- server

def on_reload(msg):
    lid = msg.get("link_id")
    im = image_by_link(lid)
    if im is None:
        return {"ok": False, "error": "no image with link %r" % lid}
    tile = int(msg.get("tile", 1001))
    key = proto.stat_from_json(msg.get("stat"))
    w = watch(lid, tile)
    if key is not None:
        w.announce(key)
    State.explicit.add((lid, tile))
    State.messages[lid] = State.messages.get(lid, 0) + 1
    return {"queued": True, "image": im.name}


def on_notice(msg):
    level = str(msg.get("level", "INFO"))
    text = str(msg.get("text", ""))[:500]
    State.notices.append((time.time(), level, text))
    del State.notices[:-20]
    print("GIMP Link: GIMP says: %s" % text)
    return {}


HANDLERS = {"reload": on_reload, "notice": on_notice}


def tick_server():
    if State.server is not None:
        try:
            if State.server.poll(0.0):
                # a reload may have been announced: check at once
                check()
        except Exception as e:  # never let the timer die
            print("GIMP Link: server error:", e)
    return SERVER_INTERVAL


def tick_watch():
    try:
        check()
    except Exception as e:
        print("GIMP Link: watcher error:", e)
    try:
        State.interval = max(0.1, float(prefs().poll_interval))
    except Exception:
        State.interval = 0.5
    return State.interval


def start():
    if State.server is None:
        State.token = proto.new_token()
        State.server = proto.LineServer(State.token, HANDLERS, APP)
    for fn, first in ((tick_server, SERVER_INTERVAL), (tick_watch, 0.5)):
        if not bpy.app.timers.is_registered(fn):
            bpy.app.timers.register(fn, first_interval=first, persistent=True)


def stop():
    for fn in (tick_server, tick_watch):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    if State.server is not None:
        State.server.close()
        State.server = None
    State.watches.clear()
    State.textures.clear()
    State.explicit.clear()


def refresh_manifests():
    """Writes Blender's current port and token into the manifests of the
    linked images (after a restart or loading a .blend)."""
    info = blender_info()
    for im in linked_images():
        path = im.gimplink.manifest
        try:
            m = proto.load_manifest(path)
        except (OSError, ValueError, proto.ProtocolError):
            continue
        if m.get("blender") != info:
            m["blender"] = info
            try:
                proto.write_json(path, m)
            except OSError:
                pass


# ---------------------------------------------------------------- GIMP

_listener_cache = [0.0, None]


def gimp_listening():
    """Whether GIMP's listener file is there (checked at most every 2 s,
    for the panel, which redraws often)."""
    now = time.monotonic()
    if now - _listener_cache[0] > 2.0:
        _listener_cache[0] = now
        _listener_cache[1] = proto.read_listener() is not None
    return _listener_cache[1]


def gimp_request(msg, timeout=3.0):
    info = proto.read_listener()
    if info is None:
        raise NotListening("GIMP is not listening (no %s)" % proto.LISTENER_FILE)
    try:
        return proto.request(info["port"], msg, token=info["token"], timeout=timeout)
    except OSError as e:
        raise NotListening("GIMP does not answer on port %s: %s" % (info["port"], e))


def launch_gimp():
    """Runs the command from the preferences, if any. Returns it."""
    cmd = (prefs().gimp_command or "").strip()
    if not cmd:
        return None
    subprocess.Popen(shlex.split(cmd), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cmd


def edit_in_gimp(image, view_layer, active=None):
    """Exports an image and asks GIMP to open it. Returns (level, text)."""
    start()
    p = prefs()
    State.textures.pop(image.gimplink.link_id, None)
    mpath, m = exporter.export(image, view_layer, blender_info(), active,
                               p.exchange_location, p.island_masks, p.dilation, mark_written)
    State.textures.pop(m["link_id"], None)
    for tile, path in textures_of(image).items():
        watch(m["link_id"], tile).mark_seen(proto.stat_key(path))
    tiles = [t["number"] for t in m["tiles"]]
    try:
        reply = gimp_request({"cmd": "open", "manifest": mpath, "tiles": tiles})
        if reply.get("ok"):
            return "INFO", "Opening %s in GIMP" % image.name
        return "WARNING", "GIMP: %s" % reply.get("error")
    except NotListening:
        proto.write_pending(m["link_id"], mpath, tiles)
        try:
            cmd = launch_gimp()
        except OSError as e:
            return "WARNING", "Could not start GIMP (%s); start it yourself" % e
        if cmd:
            return "INFO", "Starting GIMP; it opens %s when it is ready" % image.name
        return ("INFO", "Start GIMP: it opens %s when it starts "
                "(or File > Blender Link > Start Listening)" % image.name)


def pull_from_gimp(image):
    reply = gimp_request({"cmd": "send", "manifest": image.gimplink.manifest})
    if not reply.get("ok"):
        raise NotListening(reply.get("error", "GIMP refused"))


def update_uvs(image, view_layer, active=None):
    exporter.refresh_uv(image, view_layer, active, prefs().island_masks)
    try:
        gimp_request({"cmd": "uv_changed", "manifest": image.gimplink.manifest})
    except NotListening:
        pass
