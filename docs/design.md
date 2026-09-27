# Design

GIMP Link for Blender: a live texture round trip between Blender 5.x
and GIMP 3.2. "GIMP is a better editor and Krita is a better generator":
this link sits next to Blender Krita Link and Blender Layer, so the same
texture can go to Krita, GIMP and Blender in turn.

Background: [prior-art.md](prior-art.md) and the research in
`gimp-plugin-devtools/docs/interlinks.md`.

## What the user does

1. In Blender, on the active image (Image Editor, or the 3D Viewport in
   Texture Paint), **Edit in GIMP** (sidebar tab "GIMP Link", or
   Image > Edit in GIMP).
2. GIMP opens a layered XCF: the texture as the base layer, an empty
   **Paint** layer on top of it, the UV layout as a link layer, one path
   per UV island and a channel with all islands.
3. Paint. **File > Blender Link > Send to Blender** (a shortcut can be
   bound to it in Edit > Keyboard Shortcuts) writes the texture and
   Blender shows it within about half a second.
4. Next time, Edit in GIMP reopens the same XCF with its layers.

If GIMP is not running, Blender leaves the request in a pending folder;
GIMP opens it as soon as it starts (the plug-in starts with GIMP).

## Parts

```
blender/gimp_link/           Blender add-on (extension and legacy add-on)
  __init__.py                bl_info, register, operators, panels, prefs
  blender_manifest.toml
  gbl_protocol.py            shared with GIMP (identical copy)
  gbl_png.py                 shared: exact PNG reader and writer
  exporter.py                texture, UV SVG, islands, masks, manifest
  islands.py                 UV islands from bmesh, boundary loops, raster
  link.py                    socket server, watcher, reload, GIMP client
gimp/gimp-blender-link/      GIMP 3 plug-in folder
  gimp-blender-link.py       procedures (resident listener, send, listen)
  gbl_protocol.py            identical copy
  gbl_png.py                 identical copy
  gbl_gimp.py                build XCF, send, dilate (no Gimp.main)
```

`gbl_protocol.py` and `gbl_png.py` are pure Python (no bpy, no Gimp) and
tested by themselves; a test checks that both copies are identical.

## Files on disk

**Link folder** (discovery; both sides agree on it without talking):
`~/.local/share/gimp-blender-link/` on Linux, as a literal path under
`$HOME`, not `$XDG_DATA_HOME`, because a Flatpak changes that variable
per app. macOS: `~/Library/Application Support/gimp-blender-link/`;
Windows: `%APPDATA%\gimp-blender-link\`. `GIMP_BLENDER_LINK_DIR`
overrides it for both (the tests use this).

- `gimp-listener.json` (mode 0600): `{port, token, pid, version}`,
  written by the GIMP listener when it starts and removed when it stops.
- `pending/<link id>.json`: open requests left by Blender while GIMP was
  not listening; GIMP takes those younger than a day when it starts.
- `exchange/`: exchange folders for images of unsaved .blend files.

**Exchange folder** of one image: `<blend folder>/<blend name>_gimplink/<image>/`
next to a saved .blend, else `<link folder>/exchange/<image>-<id>/`.
Never under `/tmp` or Blender's temp folder: Blender's Flatpak has a
private `/tmp`. It holds:

- `manifest.json` (0600): see below.
- `texture.png` or `texture.exr`: only for images without a usable file
  of their own (generated, packed, or an unsupported format).
- `<tile>.xcf`: GIMP's working file per tile (`1001.xcf` for a plain
  image).
- `uv-<tile>.svg`: all UV faces as outlines (GIMP's link layer).
- `islands-<tile>.svg`: one closed `<path id="island-N">` per island,
  the boundary loops of the island (holes included).
- `island-ids-<tile>.png`: 16-bit grey, pixel value = island number + 1,
  0 outside every island. One file for any number of islands.
- `islands-mask-<tile>.png`: 8-bit grey, 255 inside any island.
- `backups/`: Blender's unsaved pixels, kept before a reload replaces
  them.

### manifest.json

```json
{
  "format": "gimp-blender-link", "version": 1,
  "link_id": "3f2c...", "updated": "2026-09-27T12:00:00Z",
  "image": {"name": "Albedo", "width": 2048, "height": 2048,
            "colorspace": "sRGB", "is_data": false, "is_float": false},
  "mode": "in-place",
  "tiles": [{"number": 1001, "texture": "/home/u/p/albedo.png",
             "format": "png", "bit_depth": 8, "xcf": ".../1001.xcf",
             "uv_svg": "...", "islands_svg": "...",
             "island_ids": "...", "islands_mask": "...", "islands": 6}],
  "udim": false,
  "send": {"dilation": 0},
  "blender": {"port": 41234, "token": "...", "pid": 812, "version": "5.2.0"}
}
```

All paths are absolute host paths. Both Flatpaks have
`filesystems=host`, so a path under `$HOME` is the same in both.

## Which file GIMP edits

- **In place** when the image is a file on disk (source FILE or TILED,
  not packed) in PNG, OpenEXR, TIFF, Targa, BMP or WebP. If it has
  unsaved changes in Blender, Blender first saves it with `Image.save()`
  (its own format and depth). GIMP writes back to the same file with the
  same format and depth. This is what lets Krita, GIMP and Blender share
  the texture.
- **Exchange** otherwise (generated, packed, JPEG and others): Blender
  writes the pixels to the exchange folder and points the image at that
  file (`filepath`, `source = 'FILE'`, the packed copy removed), keeping
  its colour space:
  - colour byte images: **16-bit PNG**, written by `gbl_png` from
    `image.pixels` (exact: 8-bit k becomes 257 k; no view transform, no
    colour management involved);
  - float images and data images (`is_data`, e.g. Non-Color):
    **32-bit float OpenEXR** through `Image.save(filepath=..., save_copy=True)`
    from a temporary float copy (checked exact in the spike).
  `save_render` is never used: it bakes the view transform.

Spike results behind this (headless Blender 5.2, `spikes/blender_save.py`):
`Image.save(filepath=)` on a generated image writes the file but leaves
the image with source FILE and an empty path, unusable; hence
`save_copy=True` and an explicit re-point. A byte image saves as 8-bit
PNG, a float image as 16-bit PNG, EXR keeps float32 values. Changing
`filepath` and reloading keeps the image's colour space (a fresh load of
an EXR would say Linear Rec.709).

## GIMP side

### The resident listener (spike: works)

`extension-gimp-blender-link` is a `PERSISTENT` procedure with no
arguments. GIMP starts every such procedure when it starts
(`gimp_plug_in_manager_run_extensions` in
`app/plug-in/gimppluginmanager-restore.c`), in the GUI and in
gimp-console. Its run function:

1. binds a `Gio.SocketService` on 127.0.0.1, port 29317 (or
   `GIMP_BLENDER_LINK_PORT`; if taken by another GIMP of the link, it
   says so and stops);
2. calls `procedure.persistent_ready()`: GIMP runs a nested main loop
   until this acknowledgement arrives (`gimppluginmanager-call.c`), so it
   must come first and fast;
3. calls `persistent_enable()` and runs a `GLib.MainLoop`. The socket
   callbacks run on that loop and may call the PDB.

Spike (`spikes/gimp-link-spike/`, GIMP 3.2.6 Flatpak on Broadway, a real
GUI, and in gimp-console):

- GIMP started the extension by itself; the socket answered.
- A socket request created an image and a display (`Gimp.Display.new`)
  in the running GIMP; in gimp-console `Display.new` fails cleanly.
- While a request handler was blocked for 15 s, GIMP's menus opened at
  once and another procedure of the same plug-in file ran (each menu
  procedure is its own process). Only the listener's own loop waits.
- PDB calls from the listener during GIMP's startup wait until startup
  is over (the first reply took 3.7 s).
- GIMP quit normally with the listener alive; the listener exited with
  it and the port closed.

So the listener replies at once and does the work in `GLib.idle_add`.
It never waits for Blender while Blender may be waiting for it.

A second procedure, `plug-in-gimp-blender-link-listen` (File > Blender
Link > Start Listening), is the same code as a PERSISTENT procedure
with a run-mode argument (menu procedures need one; auto-started ones
must have none). It is for a GIMP where auto-start is turned off
(`"autostart": false` in `<link folder>/gimp-settings.json`).

### Building the XCF (open)

For each requested tile:

- If an image of that XCF is already open in GIMP, present its display.
- Else if the XCF exists, load it and show it.
- Else build it:
  - precision: colour PNG → 16-bit perceptual (u16 non-linear), even for
    an 8-bit file; data PNG → 16-bit **linear**; EXR → float linear
    (half linear for a half EXR);
  - **Texture** layer: the file loaded by GIMP, its pixels copied
    **raw** into the new image through Gegl buffers
    (`R'G'B'A u16` for colour; for data the stored numbers are read as
    `R'G'B'A u16` and written as `RGBA u16`, so a data map in a linear
    image keeps its numbers and blends linearly); no colour profile
    conversion anywhere;
  - **Paint**: empty, active;
  - **UV Layout**: `Gimp.LinkLayer` of `uv-<tile>.svg`, 60 % opacity,
    content locked; GIMP reloads it when the SVG changes (spike: right
    after an atomic replace);
  - paths **Island 0 ... N** from `islands-<tile>.svg`
    (`import_paths_from_file`; ids become names; holes work: GIMP fills
    with even-odd);
  - channel **UV Islands**: the union of the island paths;
  - parasite `gimp-blender-link` on the image: manifest path, tile, link
    id, and the stat of the texture file when it was last in sync;
  - saved as `<tile>.xcf`, then displayed.
- When an XCF is reopened and the texture file's stat differs from the
  parasite (Blender or Krita changed it), the texture is added as a new
  layer "Changed outside GIMP <time>" above the paint layers, so the
  image shows the current texture and nothing is lost.

### Send to Blender

`plug-in-gimp-blender-link-send` (File > Blender Link > Send to
Blender; no dialog, so a shortcut makes it one key):

1. duplicate the image, remove the helper layers (marked by a parasite,
   so renaming them does not matter), merge the visible layers;
2. dilation by N px if the manifest (or File > Blender Link > Options)
   asks for it, see below;
3. write the texture next to the target as a temporary file, then
   `os.replace` over it: PNG through `gbl_png` from raw Gegl pixels at
   the file's depth (colour `R'G'B'A`, data `RGBA`), because GIMP's PNG
   exporter converts a linear image to sRGB (spike); EXR and other
   formats through GIMP's exporter, with the temporary name keeping the
   extension;
4. tell Blender: `{"cmd": "reload", ...}` to the manifest's port with
   Blender's token. If Blender does not answer, its poller finds the
   file anyway;
5. save the XCF (option, on by default) and update the parasite. Blender
   is told first because saving a big XCF takes a while (the end-to-end
   test found Blender's poller winning the race when the XCF came
   first).

### Dilation (seam bleed)

On the flattened copy: B = the result with everything outside the
islands cleared (selection from the island paths, inverted). Then B is
merged over shifted copies of itself: for step s in 1, 2, 4, ... (the
last step is what is left of N), a copy shifted by +s and one by -s in
x, then the same in y, each merged under B. Every sum of a subset of the
steps is a distance up to N, so this fills exactly the pixels within N
(Chebyshev distance) of an island, with the colour of an island pixel
within that distance, in 4 log2(N) merges (spike: 4 merges of a 2048²
16-bit layer took 0.23 s). Finally B is merged over the result: inside
islands nothing changes, outside the N px border nothing changes.

## Blender side

- **Names**: add-on "GIMP Link"; operators `gimplink.*`; panels
  `GIMPLINK_PT_*` in a sidebar tab "GIMP Link" (Image Editor and 3D
  Viewport); `Image.gimplink` property group; preferences under the
  add-on. Nothing is shared with Blender Krita Link
  (`Scene.global_store`, port 65431) or Blender Layer (port 65432).
- **Listener**: a non-blocking TCP socket on 127.0.0.1, port chosen by
  the system, token random per session; serviced by a `bpy.app.timers`
  job every 0.1 s on the main thread (no threads; see prior art). Its
  port and token go into every manifest; after loading a .blend or
  restarting Blender, the manifests of linked images are refreshed.
- **Watcher**: a timer (0.5 s, preference) stats each linked image's
  files by path. A change is taken when it is stable on two polls, or
  when a PNG ends with its IEND chunk, or when GIMP's `reload` names the
  exact stat on disk. Blender's own writes are remembered and ignored.
  Then `image.reload()`, the colour space is put back if it changed, and
  only the Image Editor and 3D Viewport areas are redrawn.
- **Conflicts**: if the image has unsaved changes in Blender when the
  file changes, a silent reload would lose them. On a `reload` message
  from GIMP (the user asked), Blender first saves its pixels to
  `backups/` with `save_copy`, then reloads. On a change found only by
  polling (Krita or another program), it does not reload; the panel
  shows "Changed on disk" with Reload and Ignore.
- **UVs**: from the original mesh of every mesh object in the view layer
  whose materials use the image (else the active object), active UV map.
  Islands: faces joined across edges whose two loops have the same UV
  coordinates at both ends (union-find); boundary edges chained into
  loops. Masks: triangles rasterised with numpy at pixel centres. UDIM:
  each face goes to the tile of its UV centre, in tile coordinates.
  **Update UVs** re-exports the SVG files (GIMP's link layer follows by
  itself) and asks GIMP to reload the island paths.
- **Starting GIMP**: not by default. If GIMP is not listening, Blender
  writes the pending request and says to start GIMP. Optionally a
  command in the preferences is run (e.g. `gimp` natively, or
  `flatpak-spawn --host flatpak run org.gimp.GIMP` from Flatpak Blender,
  which needs `flatpak override --user --talk-name=org.freedesktop.Flatpak
  org.blender.Blender`; that override lets Blender run any program on
  the host, and this path was not tested here because the development
  rules forbid changing overrides).
- **Pull from GIMP**: asks GIMP to run Send for this image, for users
  who stay in Blender.
- `bpy.app.online_access` is about the internet; the link only talks to
  127.0.0.1, and the manifest declares the network permission.

## Protocol

One JSON object per line, one request and one reply per TCP connection,
then close. Lines longer than 1 MiB, non-JSON, a missing or wrong token,
a wrong version or an unknown command get `{"ok": false, "error": ...}`.
`ping` needs no token and says only which app answers. Tokens are
compared with `hmac.compare_digest`.

| To | cmd | Fields | Reply |
|---|---|---|---|
| GIMP | `ping` | | `app`, `version` |
| GIMP | `open` | `manifest`, `tiles` (optional) | `queued` |
| GIMP | `send` | `manifest`, `tile` | `queued` |
| GIMP | `uv_changed` | `manifest` | `queued` |
| GIMP | `status` | | `images`: open linked images |
| Blender | `ping` | | `app`, `version` |
| Blender | `reload` | `link_id`, `tile`, `path`, `stat` | `queued` |
| Blender | `notice` | `level`, `text` | `ok` |

## Tests

`tests/run.sh`: lint (no em or en dashes, identical shared copies,
Python compiles), the protocol and PNG modules in plain Python, headless
Blender export, headless GIMP build and send, headless Blender reload
with pixel checks, an end-to-end run where the resident listener in
gimp-console and a headless Blender talk over the socket, and the
resident listener in a real GIMP on Broadway when Chrome is there.

## Found while building it

- GIMP has no call that lists displays, so an image that is already
  open cannot be brought to the front; Edit in GIMP then only takes in
  outside changes.
- A GIMP started while another GIMP GUI runs hands over to it through
  D-Bus and exits; the GUI test starts GIMP with `--new-instance`.
- In Broadway, new windows appear partly off the page; the GUI test
  moves them with the Broadway client's own `cmdMoveResizeSurface`.
  Menus work by mouse and keyboard, but popups get no keys (the action
  search and the dialog's Escape), so the test navigates menus with
  the arrow keys and clicks Cancel.
- Blender's float images give premultiplied values through
  `image.pixels` (a 16-bit PNG with alpha 5000/65535 read back as
  premultiplied); byte images give straight values. The exchange PNG
  is only written from byte images, so this does not reach the files.
- `ProcedureConfig.set_core_object_array` sets the drawables of an image
  procedure from Python; `set_property` with a list fails.
- Test isolation: Flatpak ignores `--env` for `XDG_CACHE_HOME` and
  `XDG_DATA_HOME`, so Blender's .blend thumbnails and GIMP's recent-file
  list went to the Flatpaks' own folders until the tests set them inside
  the sandbox (`flatpak run --command=env ...`). And
  `blender --command extension install-file --enable` saved preferences
  into the real Blender profile although `BLENDER_USER_CONFIG` pointed
  elsewhere; the tests install by unpacking the zip into a throwaway
  repository folder instead.

## Not in the first version

- The "better Quick Edit" (projection painting from the current view).
- Automatic UV updates while editing UVs (Update UVs is a button).
- Tested support on Windows and macOS (paths are handled, nothing was
  run there).
