# Headless Blender with a throwaway profile (GBL_THROWAWAY), after
# tools/build.sh and the extension zip unpacked into the throwaway
# user_default repository: checks that it runs as a real extension
# (bl_ext package, its preferences, operators, panels, the listener),
# and that the legacy zip installs and enables as a legacy add-on.
# Refuses to do anything if Blender's user folders are not throwaway
# ones, and never saves the preferences.
#
#   blender -b --factory-startup --python tests/blender_install.py -- LEGACY_ZIP
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys

import addon_utils
import bpy

legacy_zip = sys.argv[sys.argv.index("--") + 1]


class Results:
    # not tests/scene.py: that puts the repo's add-on on sys.path
    failures = 0

    def check(self, name, ok, detail=""):
        if not ok:
            self.failures += 1
        print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok or detail == "" else ": %s" % (detail,)),
              flush=True)

    def done(self):
        print("INSTALL failures: %d" % self.failures, flush=True)


T = Results()

throwaway = os.path.realpath(os.environ.get("GBL_THROWAWAY", "/nonexistent")) + os.sep
paths = {k: os.path.realpath(bpy.utils.user_resource(k)) + os.sep for k in ("CONFIG", "SCRIPTS", "EXTENSIONS")}
if not all(p.startswith(throwaway) for p in paths.values()):
    print("FAIL Blender's user folders are not the throwaway ones, not touching anything: %s" % paths)
    print("INSTALL failures: 1")
    sys.exit(1)
T.check("Blender's user folders are throwaway", True)
bpy.context.preferences.use_preferences_save = False
try:
    bpy.ops.extensions.repo_refresh_all()
except (AttributeError, RuntimeError):
    pass
addon_utils.enable("bl_ext.user_default.gimp_link", default_set=True)

ext = [m for m in bpy.context.preferences.addons.keys() if m.endswith(".gimp_link") and m.startswith("bl_ext.")]
T.check("extension installed and enabled", len(ext) == 1, list(bpy.context.preferences.addons.keys()))
if ext:
    mod = sys.modules[ext[0]]
    T.check("extension loaded from the extensions folder, not the repo",
            os.path.realpath(mod.__file__).startswith(os.path.realpath(bpy.utils.user_resource("EXTENSIONS"))),
            mod.__file__)
    prefs = mod.link.prefs()
    T.check("extension preferences found through __package__",
            isinstance(prefs, bpy.types.AddonPreferences) and prefs.poll_interval == 0.5, type(prefs))
    T.check("operators registered", hasattr(bpy.ops.gimplink, "edit_in_gimp")
            and hasattr(bpy.ops.gimplink, "pull_from_gimp"))
    T.check("panels registered", hasattr(bpy.types, "GIMPLINK_PT_image") and hasattr(bpy.types, "GIMPLINK_PT_view3d"))
    T.check("listener running", mod.link.State.server is not None and mod.link.State.server.port > 0)
    T.check("Image.gimplink property", hasattr(bpy.types.Image, "gimplink"))
    # disable: timers and socket gone (Blender Krita Link #14)
    addon_utils.disable(ext[0])
    T.check("disabling stops the timers and the socket", not bpy.app.timers.is_registered(mod.link.tick_server)
            and mod.link.State.server is None and not hasattr(bpy.types.Image, "gimplink"))

# the legacy add-on, into the throwaway scripts folder
bpy.ops.preferences.addon_install(filepath=legacy_zip, overwrite=True)
addon_utils.enable("gimp_link", default_set=True)
T.check("legacy add-on installs and enables", "gimp_link" in bpy.context.preferences.addons.keys(),
        list(bpy.context.preferences.addons.keys()))
leg = sys.modules.get("gimp_link")
T.check("legacy add-on loaded from the scripts folder",
        leg is not None and os.path.realpath(leg.__file__).startswith(os.path.realpath(bpy.utils.script_path_user())),
        leg and leg.__file__)
T.check("legacy add-on preferences", leg is not None and isinstance(leg.link.prefs(), bpy.types.AddonPreferences))
addon_utils.disable("gimp_link")
T.done()
