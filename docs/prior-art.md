# Prior art

How the existing links between Blender and a 2D editor work, what their
users run into, and what GIMP Link for Blender takes from them. Studied
on 2026-09-27 from the source code (local clones of the four links in
`~/store/code/links/`, shallow clones of the others) and from the issue
trackers through the GitHub API. Line numbers refer to those clones.
Anything not confirmed is marked UNVERIFIED.

No code was copied from any of these projects. GIMP Link is written from
scratch; the ideas it takes are credited below and in the README.

## Summary

| Project | Transport | What moves | Reload in Blender | UV to the 2D side | Licence |
|---|---|---|---|---|---|
| Blender Krita Link | `multiprocessing.connection` on localhost:65431 plus POSIX shared memory | raw pixels, UV polygons as data | `pixels.foreach_set` from shared memory | polygons drawn as a Krita overlay | GPL-3.0 (text only, no "or later" found) |
| Blender Layer (and V2) | TCP localhost:65432 with pickled tuples, optional shared memory | the 3D viewport rendered offscreen, into a Krita layer | not applicable (one way) | none | GPL-3.0 (text only) |
| Spritedash (Pribambase fork) | aiohttp WebSocket localhost:34613, binary messages | 8-bit pixels both ways, a rendered UV image | `pixels.foreach_set` | UV edges rendered to RGBA offscreen | GPL-3.0-or-later (2021 headers MIT) |
| GoB | shared folder in the ZBrush install, timer on one file's mtime | GoZ mesh files, BMP textures | `images.load(check_existing=True)` then `reload()` | UVs inside the mesh file | GPL-3.0-or-later |
| Auto Reload | none (watches files on disk) | nothing, it only reloads | timer on mtime, `img.reload()` | none | GPL-3.0-or-later (headers and manifest; no COPYING file) |
| Blockbench | none (external editor on the texture file) | the texture file | Node `fs.watch`, 60 ms debounce | not applicable | GPL-3.0-or-later |
| blender_psd | PSD file on disk plus Photoshop scripting (JSX through VBScript), Windows only | 8-bit layers | timer on the PSD's mtime | none | no licence file: ideas only |

## Blender Krita Link

github.com/heisenshark/blender-krita-link-plugin, last commit 2026-09-08.

- **Transport.** Blender listens with `multiprocessing.connection.Listener`
  on `localhost:65431` with the fixed authkey `b"2137"`
  (`BlenderKritaLink/connection.py:32-33`); Krita connects as the
  client. Messages are pickled Python dicts (that is what
  `multiprocessing.connection` sends). Pixels go through
  `multiprocessing.shared_memory` segments named
  `krita-blender<port>_<image>` and `blender-krita<port>`.
- **Threads.** The listener runs in a `threading.Thread` and calls
  `ImageManager.update_image()`, which does `image.pixels.foreach_set`,
  `image.update()` and even `image.pack()` from that thread
  (`connection.py:171-205`, `image_manager.py:14-83`). Blender's API is
  not thread safe; see the Spritedash notes below for the crashes this
  kind of code causes.
- **What moves.** Raw pixels in U8, U16, F16 or F32, channel swapped
  (BGR in Krita) and flipped vertically; the UV polygons of the selected
  objects as JSON-like lists (`uv_extractor.py:1836-1882`, island code
  taken from Magic UV, GPL-2.0-or-later).
- **Reload.** Not from a file: the pixels are written into the image in
  memory, so the image becomes dirty and float images are packed into
  the .blend.
- **UVs.** Drawn by Krita as an overlay widget (`uvs_viewer.py`), and
  a C++ Krita plug-in (needs a Krita rebuild) selects by UV face.
- **Sandboxes.** Shared memory needs `/dev/shm` shared between the two
  apps. Issue #23 (open): with both Flatpaks the user had to copy files
  into `/var/lib/flatpak/app/org.kde.krita/...` and run
  `flatpak override` for network and IPC. On this machine the user's
  Blender Flatpak has a `devices=shm` override, and `/dev/shm` is not
  shared between the Blender and GIMP Flatpaks.
- **Complaints.** #25 and #20 (Krita crashes on connect, unlink or
  disconnect), #21 (disconnects, the port only shown in a terminal),
  #14 (disabling the add-on crashes Blender because timers stay
  registered), #17 (its timers and redraws make other add-ons'
  handlers fire nonstop, "Can this plugin add a master switch"), #15
  (viewport corruption on an Intel GPU blamed on constant redraws), #24
  and #19 (UV overlay invisible or mis-sized).
- **Names.** `bpy.types.Scene.global_store`, operator
  `object.disconnect_operator`, panel `OBJECT_PT_KRITA` in the Image
  Editor tab "Blender Krita Link". These generic names are why GIMP
  Link prefixes everything with `gimplink`.

## Blender Layer and Blender Layer V2

github.com/Yuntokon/BlenderLayer (2024-10) and the V2 fork
github.com/JovasMotionDesigner/Blender-Leyer-V2 (2026-08, Blender 5.2).

- **Transport.** Krita is the TCP server on `localhost:65432`
  (`blender_layer/blenderLayerServer.py:100-106`); Blender connects.
  Messages are length-prefixed `pickle` dumps (`blenderLayerServer.py:7-17`,
  `blenderLayerClient.py:32-43`), after a `BLENDER_LAYER_V1` magic
  handshake. Unpickling data from a socket runs code chosen by whoever
  connects, so any local process can take over either app.
- **What moves.** One way: Blender renders its 3D view with
  `gpu.types.GPUOffScreen.draw_view3d` (`blenderLayerClient.py:864-875`)
  and sends the pixels, through shared memory `krita_blender_layer:<port>`
  or over the socket.
- **Launch.** Krita starts Blender with
  `[blenderPath, '--python', blenderLayerClient.py, '--', '--connect-to-krita', host, port]`
  (`blenderLayer.py:883`).
- **Complaints.** #1 (after a crash, `[Errno 17] File exists:
  '/krita_blender_layer:56432'`: the shared memory segment leaked, fix
  by changing the port), #2 and #9 (`[Errno 32] Broken pipe` with
  Flatpak Krita; fixed by a filesystem override or by turning shared
  memory off), #7 (Flatpak Krita cannot see `/usr/bin/blender`), #5 (the
  threads busy-loop at a full core each), #10 (`os.path.relpath` across
  Windows drives).

## Spritedash (Pribambase)

github.com/Half-Baked-Park/spritedash, a Blender 5.x fork of
github.com/AlienPolygon/pribambase (issues disabled there).

- **Transport.** Blender is an aiohttp WebSocket server on
  `localhost:34613`; Aseprite connects. Little-endian binary messages
  with a one-byte id (`docs/ARCHITECTURE.md`, "Wire protocol").
- **Event loop.** A `bpy.app.timers` callback runs one pass of an
  asyncio loop every millisecond, so all handlers run on Blender's main
  thread. Its architecture notes record two crashes fixed in the fork:
  walking `asyncio.all_tasks()` in that timer, and calling
  `modal_handler_add()` from a timer; plus sync silently dying after a
  reconnect because a listener was not re-attached.
- **What moves.** 8-bit pixels both ways (`util.update_image`,
  `pixels.foreach_set`); "Send UV" renders the UV edges into a
  `GPUOffScreen` and sends the RGBA image. Image identity is a custom
  property `sb_source` saved in the .blend.
- **Licence note.** The README says GPL-3.0-or-later and COPYING is the
  GPLv3; files from 2021 carry an MIT header from lampysprites. Both are
  compatible with GPL-3.0-or-later.

## GoB (Blender and ZBrush)

github.com/JoseConseco/GoB, GPL-3.0-or-later (`blender_manifest.toml:11`).

- **Transport.** A shared folder belonging to ZBrush: `%PUBLIC%\Pixologic`
  on Windows, `/Users/Shared/Pixologic` on macOS, nothing on Linux
  (`paths.py:27-45`; `PATH_GOZ = False`).
- **Change detection.** A persistent `bpy.app.timers` job polls the
  mtime of one list file, `GoZ_ObjectList.txt`, every 0.5 s by default
  (`gob_import.py:1366-1418`, preference `import_timer`), and imports
  when it is newer.
- **Echo suppression.** After its own export it stores the file's new
  mtime (`gob_export.py:706`), so Blender does not re-import what it
  just wrote.
- **Textures.** `bpy.data.images.load(path, check_existing=True)` and
  `image.reload()` (`gob_import.py:276-297`); `check_existing` fixed
  duplicated `.001` images (#52). Textures go to ZBrush as BMP through
  `save_render`.
- **Launch.** `open -a` on macOS, `Popen(..., shell=True)` elsewhere
  (`gob_export.py:713-722`).
- **Complaints.** Mostly paths and platforms: #295, #262, #209, #304,
  #343, #564 (ZBrush 2026 moved its folders), #288; macOS #160, #265,
  #479 (a shared folder owned by another account); Linux under Wine
  #523. And timing: **#201**, "import timer faster than progressive
  file write from zbrush export", causing several imports; #27, timers
  once crashed Blender and were replaced for a while by a modal timer.

## Auto Reload

github.com/samytichadou/Auto_Reload_Blender_addon. The files carry
GPL-3.0-or-later headers and the manifest says so; the repository has no
COPYING file.

- **Timer.** A persistent `bpy.app.timers` job, default every 5 s
  (`reload.py:282-307`, `addon_prefs.py:8-14`), plus a `load_post`
  handler.
- **Comparison.** The mtime is kept as a string custom property per
  image and compared for inequality (`reload.py:96-116`). UDIM tiles are
  expanded from `<UDIM>` and their mtimes summed (`reload.py:49-82`);
  a missing tile raises because `if os.path.isfile:` tests the function
  object (`reload.py:124`). Generated images are skipped; packed images
  are not handled (#55, open: libraries lose their packed state).
- **Mid-write.** Not handled. The maintainer on **#13**: "if you export
  a big texture from gimp, the export time can be superior to the timer
  frequency of autoreload. So the addon detects a change in the file,
  reload it, then another reload it...". His workaround is a longer
  interval.
- **Other issues.** #38 (size-based detection missed changes of the same
  size, so it went back to mtime; checksums were too slow), #19 (reload
  when Blender gets focus), #22 (UDIM), #36 and #41 (the run toggle is
  off by default).

## Blockbench's texture watcher

github.com/JannisX11/blockbench, `js/texturing/textures.js`,
GPL-3.0-or-later (`package.json:6`).

- **Watcher.** `startWatcher()` (lines 790-817) uses Node's `fs.watch`
  on the texture path, acts only on `change` events and debounces by
  60 ms, then `reloadTexture()` reads the file again with a cache
  buster. `save()` sets a `file_just_changed` flag for 100 ms before
  writing (lines 1667-1669): echo suppression again.
- **Atomic saves.** `fs.watch` follows the inode. A save that writes a
  temporary file and renames it over the texture gives one `change`
  and then silence, because the watch stays on the old inode (checked
  with Node 24 on this machine during the study; not checked inside
  Blockbench itself). A link that saves atomically, as GIMP Link does,
  must watch by path, not by inode.
- **External editor.** `openFileInEditor()` spawns the editor with the
  file; presets include GIMP's paths, on Linux a `.desktop` file that
  `spawn` probably cannot run (UNVERIFIED). Issues #1606 and #2419
  (Linux file picker wants `.exe`), #2603 (macOS `.app`), #3133 (nothing
  happens when no editor is set).

## blender_psd

github.com/heinn-dev/blender_psd. No licence file and no headers (the
manifest names GPL-3.0-or-later, but there is no licence text), so ideas
only, no code.

- Windows only (`platforms = ["windows-x64"]`), Photoshop driven by JSX
  launched through VBScript.
- Photoshop to Blender: a timer polls the PSD's mtime (1 s, faster while
  a job runs). If a Blender layer image has unsaved changes it does not
  reload but sets a disk-conflict flag and warns.
- Blender to Photoshop: dirty layers written as PNG (with an sRGB chunk
  "without it Photoshop can raise a missing-profile dialog") to a job
  folder, then a JSX job; an in-flight flag suppresses the echo of its
  own save.
- UI worth learning from: per-layer dirty markers, separate "sync from"
  and "sync to" switches, save on .blend save, conflict warnings.

## What GIMP Link takes, with credit

- **Timer poll by path, with echo suppression** (GoB, Auto Reload,
  Blockbench, blender_psd): Blender polls the texture's `os.stat` by path
  in a `bpy.app.timers` job and remembers the stat of every write it
  made itself.
- **A settle check** (the gap behind GoB #201 and Auto Reload #13): a
  change is only reloaded when the stat is the same on two polls in a
  row, the file passes a cheap completeness check (a PNG must end with
  its IEND chunk), or GIMP's `reload` message names exactly the stat
  that is on disk. GIMP writes the texture to a temporary file in the
  same folder and renames it over the old one, so a reader never sees a
  half file.
- **Conflict instead of clobber** (blender_psd): when the file changes on
  disk while the Blender image has unsaved paint, the image is not
  reloaded silently; the panel offers to reload (keeping a backup of
  Blender's pixels) or to ignore. An explicit "Send to Blender" from
  GIMP reloads, after saving that backup.
- **Stable identity saved in the .blend** (Spritedash's `sb_source`):
  each linked image keeps its link id and manifest path in a property
  group on the image.
- **Main thread only, timers unregistered on disable** (Spritedash's
  crash notes, Blender Krita Link #14, #17): no threads in Blender; the
  socket is non-blocking and serviced by a timer; nothing redraws unless
  an image was actually reloaded.
- **`check_existing`-style reload in place** (GoB #52): the existing image
  datablock is reloaded, never loaded again under a new name.
- **UV layout as vector, not pixels** (Spritedash renders it, Blender
  Krita Link draws it in a widget): GIMP Link writes SVG, which works in
  headless Blender, stays sharp at any zoom and is shown in GIMP as a
  link layer that updates when the SVG changes.

## Why the design differs

- **No shared memory.** Both Krita links depend on POSIX shared memory,
  which breaks under Flatpak (Blender Krita Link #23, Blender Layer #2
  and #9) and leaks segments after a crash (Blender Layer #1). Here
  `/dev/shm` is not shared between the Blender and GIMP Flatpaks. GIMP
  Link moves pixels only through files under `$HOME` (both Flatpaks
  have `filesystems=host`) and uses the socket only for short messages.
- **No pickle, a token instead of a fixed password.** Blender Layer
  unpickles socket data and Blender Krita Link uses pickling
  `multiprocessing.connection` with a fixed authkey. GIMP Link speaks
  one line of JSON per connection, checks a random token read from a
  file only the user can read, and closes the connection after one
  reply. Either side can restart at any time; there is no connection
  state to lose, which is what broke reconnects in Spritedash and
  Blender Krita Link (#21).
- **Files on disk, so the links can share a texture.** The Krita links
  own the pixels while connected. GIMP Link edits the texture file in
  place when there is one, so Krita (a file layer, or Blender Krita
  Link after a reload), GIMP and Blender can all use the same file, one
  after the other, without any link holding it.
- **No launching across sandboxes by default.** Blender Layer and GoB
  start the other app with `Popen`, which fails between Flatpaks
  (Blender Layer #7; tested for GIMP in the research). GIMP Link's
  GIMP side is a resident plug-in that GIMP starts by itself; Blender
  leaves a pending request that GIMP picks up when it starts. Launching
  GIMP from Blender is an opt-in command, documented with the Flatpak
  override it needs.
- **Real bit depth and colour spaces.** Spritedash and blender_psd move
  8-bit pixels. GIMP Link keeps 16-bit PNG for colour and 32-bit float
  EXR for float and Non-Color images, never bakes the view transform
  (no `save_render`), and does no profile conversion for data maps.
- **Its own names and port.** Everything in Blender is prefixed
  `gimplink`; the GIMP listener uses port 29317 (outside Linux's
  ephemeral range 32768-60999, away from 65431, 65432 and 34613); the
  Blender listener takes a free port chosen by the system, written into
  each manifest.
