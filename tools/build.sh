#!/bin/sh
# Builds the release files in dist/:
#
#   gimp_link-<version>.zip                 Blender extension (4.2 and later)
#   gimp_link-<version>-legacy-addon.zip    the same as a legacy add-on
#   gimp-blender-link-<version>-gimp.zip    the GIMP 3 plug-in folder
#
# The extension is built by Blender itself (blender --command extension
# build), the Flatpak org.blender.Blender unless BLENDER is set to a
# Blender executable. Blender runs with a throwaway profile, isolated
# from your folders (tests/isolate.sh: HOME and the XDG folders in a
# throwaway home).
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later
set -e
here=$(cd "$(dirname "$0")" && pwd)
src=$(dirname "$here")
dist=$src/dist
tmp=$src/tests/output/build
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$src/blender/gimp_link/blender_manifest.toml")
rm -rf "$tmp"
mkdir -p "$dist" "$tmp/config" "$tmp/scripts"
rm -f "$dist/gimp_link-$version.zip"
# shellcheck source=SCRIPTDIR/../tests/isolate.sh
. "$src/tests/isolate.sh"

# every user folder throwaway
if [ -n "$BLENDER" ]; then
    where=--native blender=$BLENDER
else
    where=--flatpak blender=blender
fi
gimp_run --app=org.blender.Blender "$where" --home="$tmp/home" --filesystem="$src" \
  --env=BLENDER_USER_RESOURCES="$tmp/res" --env=BLENDER_USER_CONFIG="$tmp/config" \
  --env=BLENDER_USER_SCRIPTS="$tmp/scripts" --env=BLENDER_USER_EXTENSIONS="$tmp/ext" -- \
  "$blender" --command extension build --source-dir "$src/blender/gimp_link" --output-dir "$dist"

python3 - "$src" "$dist" "$version" <<'EOF'
import os, sys, zipfile
src, dist, version = sys.argv[1:]

def pack(zpath, folder, top, skip=("__pycache__",)):
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(folder)):
            p = os.path.join(folder, name)
            if name in skip or name.endswith(".pyc") or not os.path.isfile(p):
                continue
            info = zipfile.ZipInfo.from_file(p, os.path.join(top, name))
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(p, "rb") as f:
                z.writestr(info, f.read())
    print("built", zpath)

pack(os.path.join(dist, "gimp_link-%s-legacy-addon.zip" % version),
     os.path.join(src, "blender", "gimp_link"), "gimp_link")
pack(os.path.join(dist, "gimp-blender-link-%s-gimp.zip" % version),
     os.path.join(src, "gimp", "gimp-blender-link"), "gimp-blender-link")
EOF
ls -l "$dist"
