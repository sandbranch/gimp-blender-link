# GIMP Link for Blender: edit Blender textures in GIMP 3 and see them
# back in Blender at once. The Blender half; the GIMP half is the
# gimp-blender-link plug-in.
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

bl_info = {
    "name": "GIMP Link",
    "author": "David",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "Image Editor and 3D Viewport (Texture Paint) > Sidebar > GIMP Link",
    "description": "Edit textures in GIMP 3 and see them back in Blender at once",
    "category": "Paint",
}

import time

import bpy
from bpy.app.handlers import persistent

from . import exporter
from . import gbl_protocol as proto
from . import link

VERSION = "0.1.0"


class GimpLinkImage(bpy.types.PropertyGroup):
    link_id: bpy.props.StringProperty(name="Link", description="Id of the link to GIMP")
    manifest: bpy.props.StringProperty(name="Manifest", subtype="FILE_PATH")
    folder: bpy.props.StringProperty(name="Exchange folder", subtype="DIR_PATH")
    mode: bpy.props.StringProperty(name="Mode")
    colorspace: bpy.props.StringProperty(name="Colour space")
    conflict: bpy.props.BoolProperty(
        name="Changed on disk",
        description="The file changed on disk while the image has unsaved changes in Blender")


class GimpLinkPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    exchange_location: bpy.props.EnumProperty(
        name="Exchange folder",
        description="Where files for images without a file of their own go",
        items=[("BLEND", "Next to the .blend", "A <name>_gimplink folder next to the saved .blend file"),
               ("USER", "User folder", "The GIMP Link folder in your home folder")],
        default="BLEND")
    poll_interval: bpy.props.FloatProperty(
        name="Check every", description="How often Blender looks for changed textures",
        default=0.5, min=0.1, max=10.0, unit="TIME_ABSOLUTE")
    dilation: bpy.props.IntProperty(
        name="Seam bleed", description="Pixels GIMP spreads the islands' colours outward "
        "when sending (0 = off)", default=0, min=0, max=64, subtype="PIXEL")
    island_masks: bpy.props.BoolProperty(
        name="Island masks", description="Also write island id and mask images (slower on "
        "very dense meshes)", default=True)
    gimp_command: bpy.props.StringProperty(
        name="Start GIMP with",
        description="Command that starts GIMP when it is not running (empty: start GIMP "
        "yourself). See the README for Flatpak")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "exchange_location")
        col.prop(self, "poll_interval")
        col.prop(self, "dilation")
        col.prop(self, "island_masks")
        col.prop(self, "gimp_command")
        col.label(text="Link folder: " + proto.link_dir())


def target_image(context):
    """The image to edit: the Image Editor's image, or the texture paint
    canvas in the 3D Viewport."""
    space = context.space_data
    if space is not None and space.type == "IMAGE_EDITOR" and space.image is not None:
        return space.image
    ob = context.active_object
    ts = context.scene.tool_settings if context.scene else None
    if ts is not None and ts.image_paint.mode == "IMAGE" and ts.image_paint.canvas is not None:
        return ts.image_paint.canvas
    if ob is not None and ob.active_material is not None:
        mat = ob.active_material
        imgs = getattr(mat, "texture_paint_images", [])
        i = getattr(mat, "paint_active_slot", 0)
        if 0 <= i < len(imgs):
            return imgs[i]
    return None


class GIMPLINK_OT_edit(bpy.types.Operator):
    """Edit this image in GIMP: GIMP opens it with the UV layout and islands"""
    bl_idname = "gimplink.edit_in_gimp"
    bl_label = "Edit in GIMP"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return target_image(context) is not None

    def execute(self, context):
        im = target_image(context)
        try:
            level, text = link.edit_in_gimp(im, context.view_layer, context.active_object)
        except (exporter.ExportError, OSError, RuntimeError, proto.ProtocolError) as e:
            self.report({"ERROR"}, "GIMP Link: %s" % e)
            return {"CANCELLED"}
        self.report({level}, text)
        return {"FINISHED"}


class _LinkedOperator:
    @classmethod
    def poll(cls, context):
        im = target_image(context)
        return im is not None and bool(im.gimplink.link_id)


class GIMPLINK_OT_pull(_LinkedOperator, bpy.types.Operator):
    """Ask GIMP to send this image now"""
    bl_idname = "gimplink.pull_from_gimp"
    bl_label = "Get from GIMP"

    def execute(self, context):
        try:
            link.pull_from_gimp(target_image(context))
        except link.NotListening as e:
            self.report({"WARNING"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


class GIMPLINK_OT_update_uvs(_LinkedOperator, bpy.types.Operator):
    """Write the UV layout and islands again (GIMP's UV layer follows)"""
    bl_idname = "gimplink.update_uvs"
    bl_label = "Update UVs"

    def execute(self, context):
        if context.mode == "EDIT_MESH":
            bpy.ops.object.mode_set(mode="OBJECT")
            bpy.ops.object.mode_set(mode="EDIT")
        try:
            link.update_uvs(target_image(context), context.view_layer, context.active_object)
        except (OSError, proto.ProtocolError) as e:
            self.report({"ERROR"}, "GIMP Link: %s" % e)
            return {"CANCELLED"}
        return {"FINISHED"}


class GIMPLINK_OT_reload(_LinkedOperator, bpy.types.Operator):
    """Reload the image from its file (unsaved Blender changes are kept in the backups folder)"""
    bl_idname = "gimplink.reload"
    bl_label = "Reload"

    def execute(self, context):
        kept = link.reload_image(target_image(context))
        if kept:
            self.report({"INFO"}, "Unsaved Blender pixels kept in %s" % kept)
        return {"FINISHED"}


class GIMPLINK_OT_ignore(_LinkedOperator, bpy.types.Operator):
    """Keep Blender's pixels and ignore the change on disk"""
    bl_idname = "gimplink.ignore_change"
    bl_label = "Ignore"

    def execute(self, context):
        im = target_image(context)
        im.gimplink.conflict = False
        for tile, p in link.textures_of(im).items():
            link.watch(im.gimplink.link_id, tile).mark_seen(proto.stat_key(p))
        return {"FINISHED"}


class GIMPLINK_OT_unlink(_LinkedOperator, bpy.types.Operator):
    """Stop following this image's file (the files stay where they are)"""
    bl_idname = "gimplink.unlink"
    bl_label = "Unlink"

    def execute(self, context):
        im = target_image(context)
        link.State.textures.pop(im.gimplink.link_id, None)
        for f in ("link_id", "manifest", "folder", "mode", "colorspace"):
            setattr(im.gimplink, f, "")
        im.gimplink.conflict = False
        return {"FINISHED"}


class GIMPLINK_OT_open_folder(_LinkedOperator, bpy.types.Operator):
    """Open the folder with this image's GIMP files"""
    bl_idname = "gimplink.open_folder"
    bl_label = "Open Folder"

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=target_image(context).gimplink.folder)
        return {"FINISHED"}


def draw_panel(layout, context):
    im = target_image(context)
    if im is None:
        layout.label(text="No image")
        return
    col = layout.column(align=True)
    col.label(text=im.name, icon="IMAGE_DATA")
    col.operator(GIMPLINK_OT_edit.bl_idname, icon="EXPORT")
    g = im.gimplink
    if not g.link_id:
        return
    if g.conflict:
        box = layout.box()
        box.alert = True
        box.label(text="Changed on disk", icon="ERROR")
        row = box.row(align=True)
        row.operator(GIMPLINK_OT_reload.bl_idname, text="Reload")
        row.operator(GIMPLINK_OT_ignore.bl_idname)
    col = layout.column(align=True)
    col.operator(GIMPLINK_OT_pull.bl_idname, icon="IMPORT")
    col.operator(GIMPLINK_OT_update_uvs.bl_idname, icon="UV")
    row = col.row(align=True)
    row.operator(GIMPLINK_OT_reload.bl_idname, icon="FILE_REFRESH")
    row.operator(GIMPLINK_OT_open_folder.bl_idname, text="", icon="FILE_FOLDER")
    row.operator(GIMPLINK_OT_unlink.bl_idname, text="", icon="UNLINKED")
    box = layout.box()
    box.scale_y = 0.8
    box.label(text="Editing the file in place" if g.mode == "in-place" else "Exchange copy")
    t = link.State.last_reload.get(g.link_id)
    if t:
        box.label(text="Reloaded %s" % time.strftime("%H:%M:%S", time.localtime(t)))
    if link.State.notices:
        box.label(text="GIMP: " + link.State.notices[-1][2])
    if not link.gimp_listening():
        box.label(text="GIMP is not listening", icon="INFO")


class GIMPLINK_PT_image(bpy.types.Panel):
    bl_idname = "GIMPLINK_PT_image"
    bl_label = "GIMP Link"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "GIMP Link"

    def draw(self, context):
        draw_panel(self.layout, context)


class GIMPLINK_PT_view3d(bpy.types.Panel):
    bl_idname = "GIMPLINK_PT_view3d"
    bl_label = "GIMP Link"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GIMP Link"

    @classmethod
    def poll(cls, context):
        return context.mode == "PAINT_TEXTURE"

    def draw(self, context):
        draw_panel(self.layout, context)


def image_menu(self, context):
    self.layout.separator()
    self.layout.operator(GIMPLINK_OT_edit.bl_idname, icon="EXPORT")


@persistent
def on_load_post(*_):
    link.start()
    link.mark_all_seen()
    link.refresh_manifests()


CLASSES = (GimpLinkImage, GimpLinkPreferences, GIMPLINK_OT_edit, GIMPLINK_OT_pull,
           GIMPLINK_OT_update_uvs, GIMPLINK_OT_reload, GIMPLINK_OT_ignore, GIMPLINK_OT_unlink,
           GIMPLINK_OT_open_folder, GIMPLINK_PT_image, GIMPLINK_PT_view3d)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Image.gimplink = bpy.props.PointerProperty(type=GimpLinkImage)
    bpy.types.IMAGE_MT_image.append(image_menu)
    bpy.app.handlers.load_post.append(on_load_post)
    link.start()
    # images linked in a .blend opened before the add-on was enabled
    bpy.app.timers.register(lambda: (on_load_post(), None)[1], first_interval=0.2)


def unregister():
    link.stop()
    if on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post)
    bpy.types.IMAGE_MT_image.remove(image_menu)
    del bpy.types.Image.gimplink
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
