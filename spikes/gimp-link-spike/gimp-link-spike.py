#!/usr/bin/env python3
# Spike: a resident GIMP 3.2 plug-in (a PERSISTENT procedure with no
# arguments, so GIMP starts it by itself at startup) that serves a
# Gio.SocketService on 127.0.0.1 from the plug-in's GLib main loop.
# Questions: does GIMP start it, does GIMP stay responsive, can the
# socket handler call the PDB (new image, new display), does a normal
# menu procedure of the same file still run while the resident one lives.
import gi, sys, os, json, time
gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gimp, Gio, GLib, Gegl

PORT = int(os.environ.get("SPIKE_PORT", "47850"))
LOG = os.environ.get("SPIKE_LOG", "")


def log(*a):
    line = "SPIKE %.3f %s" % (time.time(), " ".join(str(x) for x in a))
    print(line, flush=True)
    if LOG:
        with open(LOG, "a") as f:
            f.write(line + "\n")


class Spike(Gimp.PlugIn):
    def do_query_procedures(self):
        return ["extension-link-spike", "plug-in-link-spike-hello"]

    def do_create_procedure(self, name):
        if name == "extension-link-spike":
            p = Gimp.Procedure.new(self, name, Gimp.PDBProcType.PERSISTENT, self.run_resident, None)
            p.set_documentation("spike", "spike", name)
            p.set_attribution("David", "David", "2026")
            return p
        p = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, self.run_hello, None)
        p.set_image_types("*")
        p.set_menu_label("Spike Hello")
        p.add_menu_path("<Image>/Filters")
        p.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.ALWAYS)
        p.set_documentation("spike hello", "spike", name)
        p.set_attribution("David", "David", "2026")
        return p

    def run_hello(self, procedure, run_mode, image, drawables, config, data):
        log("hello ran", image.get_id() if image else None)
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def run_resident(self, procedure, config, data):
        log("resident start pid", os.getpid())
        self.loop = GLib.MainLoop()
        self.service = Gio.SocketService()
        addr = Gio.InetSocketAddress.new_from_string("127.0.0.1", PORT)
        try:
            self.service.add_address(addr, Gio.SocketType.STREAM, Gio.SocketProtocol.TCP, None)
        except GLib.Error as e:
            log("bind failed", e.message)
            procedure.persistent_ready()
            return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error())
        self.service.connect("incoming", self.on_incoming)
        self.service.start()
        procedure.persistent_ready()
        self.persistent_enable()
        log("listening", PORT)
        GLib.timeout_add(1000, self.tick)
        self.ticks = 0
        self.loop.run()
        log("loop ended")
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    def tick(self):
        self.ticks += 1
        return True

    def on_incoming(self, service, conn, source):
        stream = Gio.DataInputStream.new(conn.get_input_stream())
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line, conn)
        return True

    def on_line(self, stream, res, conn):
        line, _ = stream.read_line_finish_utf8(res)
        try:
            msg = json.loads(line or "{}")
        except Exception:
            msg = {}
        reply = self.handle(msg)
        out = conn.get_output_stream()
        out.write_all((json.dumps(reply) + "\n").encode(), None)
        conn.close(None)

    def handle(self, msg):
        cmd = msg.get("cmd")
        log("cmd", cmd)
        if cmd == "ping":
            return {"ok": True, "ticks": self.ticks, "images": len(Gimp.get_images()), "pid": os.getpid()}
        if cmd == "open":
            img = Gimp.Image.new(64, 64, Gimp.ImageBaseType.RGB)
            l = Gimp.Layer.new(img, "spike", 64, 64, Gimp.ImageType.RGB_IMAGE, 100, Gimp.LayerMode.NORMAL)
            img.insert_layer(l, None, 0)
            Gimp.context_set_foreground(Gegl.Color.new("rgb(1,0.5,0)"))
            l.edit_fill(Gimp.FillType.FOREGROUND)
            try:
                d = Gimp.Display.new(img)
                Gimp.displays_flush()
                return {"ok": True, "image": img.get_id(), "display": d.get_id() if d else None}
            except Exception as e:
                return {"ok": False, "image": img.get_id(), "error": repr(e)}
        if cmd == "busy":
            # block the plug-in's own loop for a while: GIMP must stay usable
            time.sleep(float(msg.get("s", 3)))
            return {"ok": True}
        if cmd == "quit":
            GLib.idle_add(self.loop.quit)
            return {"ok": True}
        return {"ok": False, "error": "unknown"}


Gimp.main(Spike.__gtype__, sys.argv)
