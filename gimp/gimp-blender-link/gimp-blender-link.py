#!/usr/bin/env python3
# GIMP Link for Blender: the GIMP 3 half. A resident listener that GIMP
# starts by itself (a PERSISTENT procedure with no arguments) and that
# opens textures Blender sends, plus File > Blender Link > Send to
# Blender, Options and Start Listening.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at
# your option) any later version. This program is distributed in the
# hope that it will be useful, but WITHOUT ANY WARRANTY; without even
# the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
# PURPOSE. See the GNU General Public License for more details.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi  # noqa: E402
gi.require_version("Gimp", "3.0")
from gi.repository import Gimp, Gio, GLib, GObject  # noqa: E402

import gbl_gimp  # noqa: E402
import gbl_protocol as proto  # noqa: E402

VERSION = "0.1.0"
EXTENSION = "extension-gimp-blender-link"
LISTEN = "plug-in-gimp-blender-link-listen"
SEND = "plug-in-gimp-blender-link-send"
OPTIONS = "plug-in-gimp-blender-link-options"
MENU = "<Image>/File/Blender Link"
AUTHOR = "David"
YEAR = "2026"


def settings():
    try:
        s = proto.read_json(os.path.join(proto.link_dir(), proto.SETTINGS_FILE))
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def log(*a):
    if os.environ.get("GIMP_BLENDER_LINK_DEBUG"):
        print("gimp-blender-link:", *a, flush=True)


class Listener:
    """The socket service and what it does, on the plug-in's main loop.
    Every request is answered at once; the work runs from idle
    callbacks, so the listener never waits for Blender while Blender
    waits for it."""

    def __init__(self, port):
        self.port = port
        self.token = proto.new_token()
        self.service = Gio.SocketService()
        addr = Gio.InetSocketAddress.new_from_string("127.0.0.1", port)
        self.service.add_address(addr, Gio.SocketType.STREAM, Gio.SocketProtocol.TCP, None)
        self.service.connect("incoming", self.on_incoming)
        self.handlers = {"open": self.on_open, "send": self.on_send,
                         "uv_changed": self.on_uv_changed, "status": self.on_status}

    def start(self):
        self.service.start()
        proto.write_json(os.path.join(proto.link_dir(), proto.LISTENER_FILE),
                         {"port": self.port, "token": self.token, "pid": os.getpid(),
                          "version": VERSION, "v": proto.PROTOCOL})
        for req in proto.take_pending():
            self.queue(self.do_open, req.get("manifest"), req.get("tiles"))

    def stop(self):
        self.service.stop()
        path = os.path.join(proto.link_dir(), proto.LISTENER_FILE)
        info = proto.read_listener()
        if info and info.get("token") == self.token:
            try:
                os.unlink(path)
            except OSError:
                pass

    # socket plumbing: read one line, answer, close
    def on_incoming(self, service, conn, source):
        stream = Gio.DataInputStream.new(conn.get_input_stream())
        stream.set_newline_type(Gio.DataStreamNewlineType.LF)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line, conn)
        return True

    def on_line(self, stream, res, conn):
        try:
            line, _ = stream.read_line_finish(res)
        except GLib.Error:
            line = None
        if line is None:
            reply = {"ok": False, "error": "no request"}
        else:
            reply = proto.handle_line(bytes(line), self.token, self.handlers, "gimp")
        try:
            conn.get_output_stream().write_all(proto.encode(reply), None)
            conn.close(None)
        except GLib.Error:
            pass

    def queue(self, fn, *args):
        def run():
            try:
                fn(*args)
            except Exception as e:
                Gimp.message("Blender Link: %s" % e)
            return False
        GLib.idle_add(run)

    # commands
    def _manifest(self, msg):
        path = msg.get("manifest")
        if not isinstance(path, str) or not os.path.isfile(path):
            raise gbl_gimp.LinkError("no such manifest: %r" % path)
        return path

    def on_open(self, msg):
        path = self._manifest(msg)
        self.queue(self.do_open, path, msg.get("tiles"))
        return {"queued": True}

    def do_open(self, path, tiles):
        m = proto.load_manifest(path)
        numbers = tiles or [m["tiles"][0]["number"]]
        for n in numbers:
            image, how = gbl_gimp.open_tile(path, n, display=True)
            log("open", path, n, how)
            gbl_gimp.notify_blender(m, {"cmd": "notice", "level": "INFO",
                                        "text": "%s: %s in GIMP" % (m["image"]["name"], how)})

    def on_send(self, msg):
        path = self._manifest(msg)
        tile = msg.get("tile")
        image = gbl_gimp.image_for(path, tile)
        if image is None:
            raise gbl_gimp.LinkError("that image is not open in GIMP")
        self.queue(self.do_send, image)
        return {"queued": True}

    def do_send(self, image):
        r = gbl_gimp.send(image)
        log("sent", r)

    def on_uv_changed(self, msg):
        path = self._manifest(msg)
        self.queue(gbl_gimp.uv_changed, path)
        return {"queued": True}

    def on_status(self, msg):
        out = []
        for im, info in gbl_gimp.linked_images():
            out.append({"image": im.get_id(), "name": im.get_name(), "manifest": info.get("manifest"),
                        "tile": info.get("tile"), "dirty": im.is_dirty(),
                        "layers": [l.get_name() for l in im.get_layers()],
                        "islands": len(gbl_gimp.island_paths(im))})
        return {"images": out, "version": VERSION}


class BlenderLink(Gimp.PlugIn):
    def do_query_procedures(self):
        return [EXTENSION, LISTEN, SEND, OPTIONS]

    def do_set_i18n(self, name):
        return False

    def do_create_procedure(self, name):
        if name == EXTENSION:
            p = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PERSISTENT, self.run_extension, None)
            p.set_documentation("GIMP Link for Blender: listens for Blender",
                                "Started by GIMP: opens textures sent by the GIMP Link "
                                "add-on in Blender (127.0.0.1 only).", name)
        elif name == LISTEN:
            p = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PERSISTENT, self.run_listen, None)
            p.add_enum_argument("run-mode", "Run mode", "The run mode", Gimp.RunMode,
                                Gimp.RunMode.INTERACTIVE, GObject.ParamFlags.READWRITE)
            p.set_menu_label("Start _Listening")
            p.add_menu_path(MENU)
            p.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.ALWAYS)
            p.set_documentation("Listen for Blender",
                                "Starts listening for the GIMP Link add-on in Blender, when "
                                "GIMP did not start it (see gimp-settings.json).", name)
        elif name == SEND:
            p = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, self.run_send, None)
            p.set_image_types("*")
            p.set_menu_label("_Send to Blender")
            p.add_menu_path(MENU)
            p.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE
                                   | Gimp.ProcedureSensitivityMask.DRAWABLES
                                   | Gimp.ProcedureSensitivityMask.NO_DRAWABLES)
            p.add_int_argument("dilation", "Seam bleed", "Pixels to spread the islands outward; "
                               "-1: the image's options or Blender's setting",
                               -1, 256, -1, GObject.ParamFlags.READWRITE)
            p.set_documentation("Send the texture to Blender",
                                "Writes this image's texture file for Blender (helper layers "
                                "hidden, islands optionally bled outward) and tells Blender "
                                "to reload it.", name)
        elif name == OPTIONS:
            p = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, self.run_options, None)
            p.set_image_types("*")
            p.set_menu_label("Blender Link _Options...")
            p.add_menu_path(MENU)
            p.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE
                                   | Gimp.ProcedureSensitivityMask.DRAWABLES
                                   | Gimp.ProcedureSensitivityMask.NO_DRAWABLES)
            p.add_int_argument("dilation", "Seam _bleed (px)",
                               "Pixels the islands' colours spread outward when sending; "
                               "-1 uses Blender's setting", -1, 64, -1, GObject.ParamFlags.READWRITE)
            p.add_boolean_argument("save-xcf", "Save the _XCF when sending",
                                   "Save the layered XCF each time the texture is sent",
                                   True, GObject.ParamFlags.READWRITE)
            p.set_documentation("Blender Link options of this image",
                                "Seam bleed and XCF saving for Send to Blender.", name)
        else:
            return None
        p.set_attribution(AUTHOR, AUTHOR, YEAR)
        return p

    # the resident listener
    def _listen(self, procedure, started_by_gimp):
        if started_by_gimp and settings().get("autostart", True) is False:
            procedure.persistent_ready()
            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
        port = int(settings().get("port", proto.gimp_port()))
        if "GIMP_BLENDER_LINK_PORT" in os.environ:
            port = proto.gimp_port()
        try:
            listener = Listener(port)
        except GLib.Error as e:
            procedure.persistent_ready()
            msg = "Blender Link: cannot listen on 127.0.0.1:%d (%s)" % (port, e.message)
            try:
                info = proto.read_listener()
                if info and proto.request(info["port"], {"cmd": "ping"}, timeout=1.0).get("app") == "gimp":
                    msg = "Blender Link is already listening on port %s" % info["port"]
            except (OSError, proto.ProtocolError):
                pass
            if not started_by_gimp:
                Gimp.message(msg)
            print(msg, flush=True)
            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
        # GIMP waits for this acknowledgement before it goes on
        procedure.persistent_ready()
        self.persistent_enable()
        listener.start()
        log("listening on", port)
        if not started_by_gimp:
            Gimp.message("Blender Link is listening on port %d" % port)
        loop = GLib.MainLoop()
        loop.run()
        listener.stop()
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def run_extension(self, procedure, config, data):
        return self._listen(procedure, True)

    def run_listen(self, procedure, config, data):
        return self._listen(procedure, False)

    def run_send(self, procedure, run_mode, image, drawables, config, data):
        try:
            r = gbl_gimp.send(image, config.get_property("dilation"))
        except (gbl_gimp.LinkError, proto.ProtocolError, OSError) as e:
            if run_mode == Gimp.RunMode.INTERACTIVE:
                Gimp.message("Send to Blender: %s" % e)
            return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                               GLib.Error.new_literal(GLib.quark_from_string("gbl"), str(e), 0))
        if not r["blender"] and run_mode == Gimp.RunMode.INTERACTIVE:
            Gimp.message("Sent to %s. Blender did not answer; it will find the file "
                         "when it runs with GIMP Link." % r["texture"])
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def run_options(self, procedure, run_mode, image, drawables, config, data):
        info = gbl_gimp.read_link(image)
        if not info:
            if run_mode == Gimp.RunMode.INTERACTIVE:
                Gimp.message("This image is not linked to Blender.")
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
        if run_mode == Gimp.RunMode.INTERACTIVE:
            # the dialog starts from this image's options; called from a
            # script, the arguments are the options
            opts = info.get("options") or {}
            config.set_property("dilation", int(opts.get("dilation", -1)))
            config.set_property("save-xcf", bool(opts.get("save_xcf", True)))
            gi.require_version("GimpUi", "3.0")
            from gi.repository import GimpUi
            GimpUi.init("gimp-blender-link")
            dialog = GimpUi.ProcedureDialog.new(procedure, config, "Blender Link Options")
            dialog.fill(None)
            ok = dialog.run()
            dialog.destroy()
            if not ok:
                return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
        info["options"] = {"dilation": config.get_property("dilation"),
                           "save_xcf": config.get_property("save-xcf")}
        gbl_gimp.write_link(image, info)
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


if __name__ == "__main__":
    Gimp.main(BlenderLink.__gtype__, sys.argv)
