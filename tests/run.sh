#!/bin/sh
# Tests GIMP Link for Blender. One command, no network, no display:
#
#   tests/run.sh           everything (the GUI test only if Chrome is there)
#   GBL_GUI=0 tests/run.sh without the GUI test
#
# Steps: lint (no em or en dashes, identical shared modules, Python
# compiles, the Blender extension manifest validates), unit tests of the
# protocol and PNG modules in plain Python, headless Blender export,
# headless GIMP build and send, headless Blender reload, an end-to-end
# run of the resident listener in gimp-console with a headless Blender,
# and the resident listener in a real GIMP on Broadway (tests/gui).
#
# Blender and GIMP are the Flatpaks org.blender.Blender and
# org.gimp.GIMP (GBL_BLENDER_APP, GBL_GIMP_APP to change). They run
# with throwaway profiles under tests/output (BLENDER_USER_CONFIG,
# BLENDER_USER_SCRIPTS, GIMP3_DIRECTORY, and XDG_DATA_HOME and
# XDG_CACHE_HOME so that no thumbnails or recent-file entries land in
# the Flatpaks' own folders) and a throwaway link folder and
# port (GIMP_BLENDER_LINK_DIR, GIMP_BLENDER_LINK_PORT): your own
# Blender, GIMP and their add-ons and plug-ins are not used or changed.
#
# Prints PASS or FAIL for each case; exits non-zero if any case fails.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later
here=$(cd "$(dirname "$0")" && pwd)
src=$(dirname "$here")
out=$here/output
run=$out/run
blender_app=${GBL_BLENDER_APP:-org.blender.Blender}
gimp_app=${GBL_GIMP_APP:-org.gimp.GIMP}
status=0

rm -rf "$run"
mkdir -p "$run/blender/config" "$run/blender/scripts" "$run/tmp" "$out/profile-console"

# counts the PASS and FAIL lines of a log, prints them, and checks that
# the script got to its "<name> failures: 0" line
report () {
    log=$1
    name=$2
    grep -E "^(PASS|FAIL)" "$log"
    if ! grep -q "^$name failures: 0$" "$log"; then
        grep -E "Traceback|Error|^  File" "$log" | head -20
        echo "FAIL $name: did not finish cleanly, see $log"
        status=1
    fi
}

# Blender with every user folder under the throwaway folder $1
# (BLENDER_USER_*, and XDG_CACHE_HOME and XDG_DATA_HOME, which Flatpak
# does not let --env change, so they are set inside the sandbox); link
# folder $2, port $3; the rest are Blender's arguments
blender_in () {
    home=$1
    linkdir=$2
    port=$3
    shift 3
    timeout 600 flatpak run --filesystem="$src" \
      --env=BLENDER_USER_RESOURCES="$home/res" --env=BLENDER_USER_CONFIG="$home/config" \
      --env=BLENDER_USER_SCRIPTS="$home/scripts" --env=BLENDER_USER_EXTENSIONS="$home/ext" \
      --env=GIMP_BLENDER_LINK_DIR="$linkdir" --env=GIMP_BLENDER_LINK_PORT="$port" \
      --env=TEST_EXPORTED="$run/e2e/exported" --env=TEST_DONE="$run/e2e/done" \
      --env=GBL_THROWAWAY="$home" \
      --command=env "$blender_app" XDG_CACHE_HOME="$home/cache" XDG_DATA_HOME="$home/data" \
      blender "$@"
}

blender () {
    blender_in "$run/blender" "$1" "$2" -b --factory-startup --python "$3" -- "$4"
}

# gimp-console with the profile $1 and its XDG folders next to it
gimp_console () {
    profile=$1
    linkdir=$2
    port=$3
    shift 3
    timeout 600 flatpak run --filesystem="$src" --env=GIMP3_DIRECTORY="$profile" \
      --env=GIMP_BLENDER_LINK_DIR="$linkdir" --env=GIMP_BLENDER_LINK_PORT="$port" \
      --env=TEST_HERE="$here" --env=TEST_WORK="$run/work" --env=TEST_DONE="$run/e2e/done" \
      --command=env "$gimp_app" XDG_DATA_HOME="$profile-xdg/data" XDG_CACHE_HOME="$profile-xdg/cache" \
      gimp-console-3.2 --no-interface --no-data --no-fonts --batch-interpreter python-fu-eval "$@" --quit
}

free_port () {
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

echo "== lint"
python3 - "$src" <<'EOF' >"$run/lint.log" 2>&1
import os, py_compile, sys, tempfile
src = sys.argv[1]
bad = []
texts = (".py", ".md", ".sh", ".toml", ".mjs", ".json", ".txt", ".desktop", ".svg")
for root, dirs, files in os.walk(src):
    dirs[:] = [d for d in dirs if d not in (".git", "output", "__pycache__", "dist")]
    for f in files:
        p = os.path.join(root, f)
        if f.endswith(texts) or f in ("COPYING", ".gitignore"):
            text = open(p, encoding="utf-8").read()
            for n, line in enumerate(text.splitlines(), 1):
                if chr(0x2014) in line or chr(0x2013) in line:
                    bad.append("%s:%d" % (os.path.relpath(p, src), n))
print("PASS no em or en dashes" if not bad else "FAIL em or en dashes: " + " ".join(bad))
fails = int(bool(bad))
for f in ("gbl_protocol.py", "gbl_png.py"):
    a = open(os.path.join(src, "blender", "gimp_link", f), "rb").read()
    b = open(os.path.join(src, "gimp", "gimp-blender-link", f), "rb").read()
    print(("PASS " if a == b else "FAIL ") + "%s identical in Blender and GIMP" % f)
    fails += a != b
cache = tempfile.mkdtemp()
errs = []
for root, dirs, files in os.walk(src):
    dirs[:] = [d for d in dirs if d not in (".git", "output", "__pycache__", "dist")]
    for f in files:
        if f.endswith(".py"):
            try:
                py_compile.compile(os.path.join(root, f), cfile=os.path.join(cache, "x.pyc"), doraise=True)
            except py_compile.PyCompileError as e:
                errs.append(str(e))
print("PASS Python compiles" if not errs else "FAIL Python: " + "; ".join(errs))
fails += bool(errs)
mode = os.stat(os.path.join(src, "gimp", "gimp-blender-link", "gimp-blender-link.py")).st_mode
print(("PASS " if mode & 0o111 else "FAIL ") + "GIMP plug-in file is executable")
fails += not (mode & 0o111)
print("LINT failures: %d" % fails)
EOF
if blender_in "$run/blender" "$run/linkdir" 0 --command extension validate "$src/blender/gimp_link" \
     >"$run/validate.log" 2>&1 &&
   grep -q "Success" "$run/validate.log"; then
    echo "PASS Blender extension manifest validates" >>"$run/lint.log"
else
    echo "FAIL Blender extension manifest: $(tail -3 "$run/validate.log")" >>"$run/lint.log"
    sed -i 's/^LINT failures: \([0-9]*\)$/LINT failures: 1\1/' "$run/lint.log"
fi
report "$run/lint.log" LINT

echo "== build and install (extension and legacy add-on, throwaway Blender)"
inst=$run/install
mkdir -p "$inst"
"$src/tools/build.sh" >"$run/build.log" 2>&1 || { tail -5 "$run/build.log"; echo "FAIL build"; status=1; }
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$src/blender/gimp_link/blender_manifest.toml")
# installed the way Blender installs an extension (unpacked into a
# repository folder), in a throwaway Blender that checks its own paths
# first; no "blender --command extension install-file": its --enable
# saved the preferences into the real Blender profile despite
# BLENDER_USER_CONFIG
mkdir -p "$inst/ext/user_default"
python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
  "$src/dist/gimp_link-$version.zip" "$inst/ext/user_default/gimp_link"
blender_in "$inst" "$inst/linkdir" 0 -b --factory-startup --python "$here/blender_install.py" -- \
  "$src/dist/gimp_link-$version-legacy-addon.zip" >"$run/install.log" 2>&1
report "$run/install.log" INSTALL

echo "== unit (plain Python)"
TEST_TMP="$run/tmp" timeout 300 python3 "$here/unit_test.py" >"$run/unit.log" 2>&1
report "$run/unit.log" UNIT

echo "== Blender: export"
mkdir -p "$run/work"
blender "$run/linkdir" "$(free_port)" "$here/blender_export.py" "$run/work" >"$run/blender-export.log" 2>&1
report "$run/blender-export.log" "BLENDER EXPORT"

echo "== GIMP: build and send"
# the plug-in installed in the throwaway profile, for its procedures
cplug=$out/profile-console/plug-ins/gimp-blender-link
mkdir -p "$cplug"
rm -f "$cplug"/*.py
cp "$src"/gimp/gimp-blender-link/*.py "$cplug/"
chmod +x "$cplug/gimp-blender-link.py"
gimp_console "$out/profile-console" "$run/linkdir" "$(free_port)" \
  -b "exec(open('$here/gimp_build_send.py').read())" >"$run/gimp.log" 2>&1
report "$run/gimp.log" "GIMP BUILD SEND"

echo "== Blender: reload"
blender "$run/linkdir" "$(free_port)" "$here/blender_reload.py" "$run/work" >"$run/blender-reload.log" 2>&1
report "$run/blender-reload.log" "BLENDER RELOAD"

echo "== end to end: resident listener in gimp-console and Blender"
profile=$out/profile-e2e
plugdir=$profile/plug-ins/gimp-blender-link
mkdir -p "$plugdir" "$run/e2e/work"
rm -f "$plugdir"/*.py
cp "$src"/gimp/gimp-blender-link/*.py "$plugdir/"
chmod +x "$plugdir/gimp-blender-link.py"
port=$(free_port)
blender "$run/e2e/linkdir" "$port" "$here/e2e_blender.py" "$run/e2e/work" >"$run/e2e-blender.log" 2>&1 &
blender_pid=$!
i=0
while [ ! -e "$run/e2e/exported" ] && [ $i -lt 600 ] && kill -0 "$blender_pid" 2>/dev/null; do
    sleep 0.2
    i=$((i + 1))
done
GIMP_BLENDER_LINK_DEBUG=1 gimp_console "$profile" "$run/e2e/linkdir" "$port" \
  -b "exec(open('$here/gimp_wait.py').read())" >"$run/e2e-gimp.log" 2>&1 &
gimp_pid=$!
wait "$blender_pid"
touch "$run/e2e/done"
wait "$gimp_pid"
report "$run/e2e-blender.log" E2E
if grep -E "gimp-blender-link.*(CRITICAL|WARNING)|Traceback" "$run/e2e-gimp.log"; then
    echo "FAIL e2e: GIMP printed errors, see $run/e2e-gimp.log"
    status=1
fi

if [ "${GBL_GUI:-1}" != 0 ]; then
    echo "== GUI: resident listener in GIMP on Broadway"
    "$here/gui/gui-test.sh" >"$run/gui.log" 2>&1
    gui_status=$?
    if grep -q "^GUI SKIP" "$run/gui.log"; then
        grep "^GUI SKIP" "$run/gui.log"
    else
        report "$run/gui.log" GUI
        [ $gui_status -eq 0 ] || status=1
    fi
fi

pass=$(cat "$run"/*.log | grep -c "^PASS")
fail=$(cat "$run"/*.log | grep -c "^FAIL")
echo "== $pass passed, $fail failed"
[ "$fail" -eq 0 ] || status=1
exit $status
