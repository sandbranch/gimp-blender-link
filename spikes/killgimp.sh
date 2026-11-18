#!/bin/sh
# stop only the GIMP processes whose command line contains $1 (their
# plug-ins first)
for pid in $(pgrep -x gimp-3.2; pgrep -x gimp-console-3.2); do
  if tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | grep -qF -- "$1"; then
    echo "stopping $pid"; pkill -P "$pid"; kill "$pid"
  fi
done
