#!/bin/sh
# The GIMP Link listener in a real GIMP (Flatpak GUI on Broadway),
# looked at with a headless Chrome. Makes the manifests with a headless
# Blender export first. Screenshots in tests/output/gui/.
#
#   tests/gui/gui-test.sh
#
# Skips (exit 0) without Chrome or node 22. Throwaway GIMP profile,
# link folder and port; GIMP and Blender run isolated from your folders
# (gimp-plugin-devtools/gimp-run.sh: HOME and the XDG folders in a
# throwaway home, so no recent-file entries, thumbnails or GIO metadata
# in the Flatpaks' own folders); stops only the GIMP and Chrome it
# started.
# Needs ../gimp-plugin-devtools (or GIMP_PLUGIN_DEVTOOLS) for cdp.mjs.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later
here=$(cd "$(dirname "$0")" && pwd)
tests=$(dirname "$here")
src=$(dirname "$tests")
out=$tests/output/gui
devtools=${GIMP_PLUGIN_DEVTOOLS:-$src/../gimp-plugin-devtools}

command -v google-chrome >/dev/null || command -v chromium >/dev/null ||
  command -v chromium-browser >/dev/null || { echo "GUI SKIP: no Chrome or Chromium"; exit 0; }
node -e 'process.exit(typeof WebSocket === "undefined" ? 1 : 0)' 2>/dev/null ||
  { echo "GUI SKIP: needs node 22 or later"; exit 0; }
[ -f "$devtools/gui/cdp.mjs" ] || { echo "GUI SKIP: no $devtools/gui/cdp.mjs"; exit 0; }
# shellcheck source=SCRIPTDIR/../isolate.sh
. "$tests/isolate.sh"

rm -rf "$out"
mkdir -p "$out/work" "$out/blender/config" "$out/blender/scripts"
profile=$tests/output/profile-gui
plugdir=$profile/plug-ins/gimp-blender-link
mkdir -p "$plugdir"
rm -f "$plugdir"/*.py
cp "$src"/gimp/gimp-blender-link/*.py "$plugdir/"
chmod +x "$plugdir/gimp-blender-link.py"
# one window at the top left, no welcome dialog
cat >"$profile/sessionrc" <<'EOF'
(session-info "toplevel"
    (factory-entry "gimp-single-image-window")
    (position 0 0)
    (size 1360 860))
(single-window-mode yes)
EOF
grep -q show-welcome-dialog "$profile/gimprc" 2>/dev/null ||
  echo '(show-welcome-dialog no)' >>"$profile/gimprc"

port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
b=$out/blender
gimp_run --timeout=300 --app="${GBL_BLENDER_APP:-org.blender.Blender}" --flatpak \
  --home="$b/home" --filesystem="$src" \
  --env=BLENDER_USER_RESOURCES="$b/res" --env=BLENDER_USER_CONFIG="$b/config" \
  --env=BLENDER_USER_SCRIPTS="$b/scripts" --env=BLENDER_USER_EXTENSIONS="$b/ext" \
  --env=GIMP_BLENDER_LINK_DIR="$out/linkdir" --env=GIMP_BLENDER_LINK_PORT="$port" \
  -- blender -b --factory-startup \
  --python "$tests/blender_export.py" -- "$out/work" >"$out/blender.log" 2>&1
grep -q "^BLENDER EXPORT failures: 0$" "$out/blender.log" ||
  { echo "FAIL GUI: the Blender export for the GUI test failed, see $out/blender.log"; echo "GUI failures: 1"; exit 1; }
rm -rf "$out/linkdir/pending"

GUI_OUT=$out GUI_WORK=$out/work GUI_PROFILE=$profile GIMP_PLUGIN_DEVTOOLS=$devtools \
  GIMP_BLENDER_LINK_DIR=$out/linkdir GIMP_BLENDER_LINK_PORT=$port \
  timeout 900 python3 "$here/gui_test.py"
