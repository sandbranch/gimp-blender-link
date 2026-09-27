# Spikes

Throwaway experiments run before the design, kept for the record; the
results are in [../docs/design.md](../docs/design.md).

- `blender_save.py`: what `Image.save()` writes from headless Blender
  5.2 and what a filepath change plus reload does to pixels and colour
  space.
- `gimp_api.py`: GIMP 3.2 API pieces (export arguments, path import,
  raw Gegl buffer access, PNG export of a linear image, EXR, merge
  timing, link layer refresh), run with gimp-console and python-fu-eval.
- `gimp-link-spike/`: a resident plug-in (a PERSISTENT procedure with no
  arguments, started by GIMP) serving a `Gio.SocketService`, tested in
  gimp-console and in the GIMP GUI on Broadway with `client.py`.
- `killgimp.sh`: stops only the GIMP Flatpak instances whose command
  line contains a given string.
