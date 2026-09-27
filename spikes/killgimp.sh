#!/bin/sh
# stop only the GIMP Flatpak instances whose command line contains $1
for i in $(flatpak ps --columns=instance,child-pid,application | awk '$3=="org.gimp.GIMP"{print $1":"$2}'); do
  inst=${i%:*}; pid=${i#*:}
  if tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | grep -qF -- "$1"; then echo "stopping $inst"; flatpak kill "$inst"; fi
done
