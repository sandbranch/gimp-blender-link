# The resident listener in a real GIMP (the Flatpak, GUI on a Broadway
# display, looked at through a headless Chrome with
# gimp-devtools/gui/cdp.mjs). This script plays Blender: a
# stand-in listener gets GIMP's reload messages.
#
# 1. GIMP starts; the listener starts by itself and answers.
# 2. open: GIMP builds the XCF and shows it in a window (screenshot).
# 3. GIMP stays usable: its menus open while the link is linked.
# 4. send over the socket: the texture is written, Blender is told.
# 5. Send to Blender from GIMP's own menu (File > Blender Link), as a
#    user would run it.
# 6. The Options dialog opens from the menu and closes with Cancel.
#
# Needs the manifests of a Blender export (tests/gui/gui-test.sh makes
# them). Stops the GIMP and the Chrome it started, and nothing else.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

here = os.path.dirname(os.path.abspath(__file__))
tests = os.path.dirname(here)
src = os.path.dirname(tests)
sys.path.insert(0, os.path.join(src, "blender", "gimp_link"))
import gbl_protocol as proto  # noqa: E402

out = os.environ["GUI_OUT"]
work = os.environ["GUI_WORK"]
linkdir = os.environ["GIMP_BLENDER_LINK_DIR"]
port = int(os.environ["GIMP_BLENDER_LINK_PORT"])
profile = os.environ["GUI_PROFILE"]
devtools = os.environ.get("GIMP_PLUGIN_DEVTOOLS", os.path.join(src, "..", "gimp-devtools"))
cdp_js = os.path.join(devtools, "gui", "cdp.mjs")
failures = 0


def check(name, ok, detail=""):
    global failures
    if ok:
        print("PASS %s" % name, flush=True)
    else:
        failures += 1
        print("FAIL %s%s" % (name, (": %s" % (detail,)) if detail != "" else ""), flush=True)
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


broadway = free_port()
display = ":%d" % (20 + broadway % 70)
cdp_port = free_port()
env = dict(os.environ, CDP_PORT=str(cdp_port))


def cdp(*actions, timeout=90):
    r = subprocess.run(["node", cdp_js] + list(actions), env=env, capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


chrome_bin = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
chrome = gimp = None
fake = None
stop = threading.Event()
got = []
view = "size:1400,900"


def our_gimp_instances():
    """Flatpak instances of GIMP whose command line names our profile."""
    r = subprocess.run(["flatpak", "ps", "--columns=instance,child-pid,application"],
                       capture_output=True, text=True)
    found = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[2] != "org.gimp.GIMP":
            continue
        try:
            cmd = open("/proc/%s/cmdline" % parts[1], "rb").read().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if profile in cmd or ("--port %d" % broadway) in cmd:
            found.append(parts[0])
    return found


def cleanup():
    stop.set()
    if chrome is not None:
        try:
            cdp("nav:about:blank", timeout=10)
        except Exception:
            pass
    for inst in our_gimp_instances():
        subprocess.run(["flatpak", "kill", inst])
    if gimp is not None:
        try:
            gimp.wait(10)
        except subprocess.TimeoutExpired:
            gimp.kill()
    if chrome is not None:
        chrome.send_signal(signal.SIGTERM)
        try:
            chrome.wait(10)
        except subprocess.TimeoutExpired:
            chrome.kill()
    if fake is not None:
        fake.close()


try:
    manifests = json.load(open(os.path.join(work, "manifests.json")))
    mpath = manifests["albedo"]
    m = proto.load_manifest(mpath)
    # the stand-in for Blender
    fake = proto.LineServer("gui-token", {"reload": lambda msg: got.append(msg) or {"queued": True},
                                          "notice": lambda msg: {}}, "blender")
    threading.Thread(target=lambda: [fake.poll(0.05) for _ in iter(stop.is_set, True)], daemon=True).start()
    m["blender"] = {"port": fake.port, "token": "gui-token", "pid": 0, "version": "gui-test"}
    proto.write_json(mpath, m)

    chrome = subprocess.Popen([chrome_bin, "--headless=new", "--remote-debugging-port=%d" % cdp_port,
                               "--user-data-dir=%s" % os.path.join(out, "chrome"), "--password-store=basic",
                               "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log = open(os.path.join(out, "gimp.log"), "w")
    # broadwayd first, then (after Chrome shows the page) GIMP
    # isolated from the user's folders by gimp-run.sh: HOME and the XDG
    # folders in a throwaway home (no recent-file entries, thumbnails or
    # GIO metadata in GIMP's own folders)
    gimp = subprocess.Popen([
        os.path.join(devtools, "gimp-run.sh"), "--flatpak", "--home=%s" % os.path.join(out, "home"),
        "--filesystem=%s" % src,
        "--env=GDK_BACKEND=broadway", "--env=BROADWAY_DISPLAY=%s" % display,
        "--env=GIMP3_DIRECTORY=%s" % profile,
        "--env=GIMP_BLENDER_LINK_DIR=%s" % linkdir, "--env=GIMP_BLENDER_LINK_PORT=%d" % port,
        "--env=GIMP_BLENDER_LINK_DEBUG=1",
        "--", "sh", "-c",
        "broadwayd --port %d %s & bw=$!; trap 'kill $bw' EXIT; sleep 5; "
        "gimp-3.2 --new-instance --no-splash --no-fonts"
        % (broadway, display)],
        stdout=log, stderr=subprocess.STDOUT)
    time.sleep(2.5)
    rc, text = cdp(view, "nav:http://127.0.0.1:%d/" % broadway, "wait:500")
    check("Chrome shows the Broadway page", rc == 0, text)

    def up():
        try:
            return proto.request(port, {"cmd": "ping"}, timeout=1).get("app") == "gimp"
        except OSError:
            return False

    end = time.time() + 240
    while time.time() < end and not up():
        time.sleep(0.5)
    check("GIMP GUI: the listener started by itself", up())
    info = proto.read_listener(linkdir) or {}
    token = info.get("token")

    def gimp_req(msg, timeout=5):
        return proto.request(port, msg, token=token, timeout=timeout)

    def status():
        try:
            return gimp_req({"cmd": "status"}).get("images", [])
        except OSError:
            return []

    r = gimp_req({"cmd": "open", "manifest": mpath})
    check("open accepted", r.get("queued") is True, r)
    end = time.time() + 60
    while time.time() < end and not status():
        time.sleep(0.3)
    st = status()
    check("GIMP GUI: the linked image is open", len(st) == 1 and st[0]["layers"] ==
          ["UV Layout", "UV Islands", "Paint", "Texture"], st)
    check("GIMP GUI: the XCF was saved", os.path.isfile(m["tiles"][0]["xcf"]))
    # Broadway puts new windows partly off the page: move them into view
    time.sleep(2)
    move = ("eval:(() => { for (const s of Object.values(surfaces)) { if (s.x < 0 || s.y < 0) {"
            " cmdMoveResizeSurface(s.id, true, 0, 0, false, 0, 0); sendConfigureNotify(s); } }"
            " return Object.keys(surfaces).length })()")
    # the first start of a GIMP version shows a welcome dialog: Escape
    cdp(view, move, "wait:800", "key:Escape", "wait:800")
    rc, text = cdp(view, move, "wait:1500", "shot:%s" % os.path.join(out, "1-opened.png"))
    check("screenshot of the opened image", rc == 0 and os.path.isfile(os.path.join(out, "1-opened.png")), text)

    # GIMP stays usable: the File menu opens (and shows Blender Link)
    rc, text = cdp(view, "click:45,72", "wait:1200", "shot:%s" % os.path.join(out, "2-file-menu.png"),
                   "key:Escape", "wait:300")
    check("GIMP's menus work with the listener running", rc == 0, text)

    before = proto.stat_key(m["tiles"][0]["texture"])
    n0 = len(got)
    r = gimp_req({"cmd": "send", "manifest": mpath})
    check("send accepted", r.get("queued") is True, r)
    end = time.time() + 30
    while time.time() < end and len(got) == n0:
        time.sleep(0.1)
    check("send over the socket: Blender told", len(got) == n0 + 1 and got[-1]["link_id"] == m["link_id"], got)
    after = proto.stat_key(m["tiles"][0]["texture"])
    check("send over the socket: texture written", after != before and proto.looks_complete(m["tiles"][0]["texture"]))

    # Send to Blender from GIMP's own menu: File > Blender Link, then the
    # item's mnemonic (S), as a user would
    n1 = len(got)
    rc, text = cdp(view, "click:45,72", "wait:1000", "key:Up", "wait:400", "key:Right", "wait:1200",
                   "shot:%s" % os.path.join(out, "3-blender-link-menu.png"), "key:s", "wait:500")
    end = time.time() + 30
    while time.time() < end and len(got) == n1:
        time.sleep(0.1)
    check("File > Blender Link > Send to Blender reaches Blender", len(got) == n1 + 1, (rc, text, len(got)))
    cdp(view, "wait:500", "shot:%s" % os.path.join(out, "4-after-send.png"))
    st = status()
    check("image not dirty after sending (XCF saved)", st and not st[0]["dirty"], st)

    # the Options dialog (GimpUi): File > Blender Link > Options, then Cancel
    count = "eval:Object.values(surfaces).filter(s => s.visible !== false && s.width > 200).length"
    rc0, n0 = cdp(view, count)
    rc, text = cdp(view, "click:45,72", "wait:1000", "key:Up", "wait:400", "key:Right", "wait:800", "key:o",
                   "wait:4000", count, "shot:%s" % os.path.join(out, "5-options.png"))
    n1 = text.splitlines()[-1] if text else ""
    check("Options dialog opens", rc == 0 and n1.isdigit() and n0.isdigit() and int(n1) > int(n0), (n0, text))
    # Cancel: on Broadway the dialog has no keyboard focus, so it is
    # clicked, at its place in the dialog's own window (the topmost one)
    rc, at = cdp(view, "eval:(() => { const s = Object.values(surfaces).filter(s => s.width > 200)"
                 ".sort((a, b) => (Number(b.canvas.style.zIndex) || 0) - (Number(a.canvas.style.zIndex) || 0))[0];"
                 " return s.x + ',' + s.y })()")
    try:
        dx, dy = (int(v) for v in at.splitlines()[-1].split(","))
        cdp(view, "click:%d,%d" % (dx + 278, dy + 204), "wait:1500")  # 26, 23: the window shadow
    except ValueError:
        pass
    rc, n2 = cdp(view, count)
    check("Options dialog closes with Cancel", n2 == n0, (n0, n2, at))
    check("GIMP still answers after the dialog", bool(status()))
except Exception as e:
    import traceback
    traceback.print_exc()
    check("GUI test ran", False, e)
finally:
    cleanup()
    time.sleep(1)
    check("our GIMP stopped", not our_gimp_instances())

print("GUI failures: %d" % failures, flush=True)
sys.exit(1 if failures else 0)
