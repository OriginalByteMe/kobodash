#!/bin/sh
# Boot path of the dashboard, run by /usr/local/kobodash/boot.sh early at every boot.
# In order: uninstall if asked, keep NiLuJe's open servers off (and run key-only SSH if the user
# opted in), stay in Nickel when told to, then hand the screen from Nickel to trmnl.sh.
#
# Ways back to the normal Kobo, none of which need SSH:
#   - power button while the Kobo is asleep on battery: trmnlloop.sh writes `nickel-once` and
#     reboots; the next boot (this script) deletes the file and stays in Nickel, just once.
#   - an empty file named `disabled` next to this script keeps the Kobo in Nickel until removed.
#   - three boots in a row that never reach a render (bootcount) also stay in Nickel.
#   - `make uninstall` writes `uninstall`, see below.
cd "$(dirname "$0")" || exit 1
R=${KOBODASH_ROOT:-} # only the tests set this: a prefix for every absolute path below
USBNET=$R/usr/local/niluje/usbnet/etc

if [ -e uninstall ]; then
    # Written by `make uninstall`: remove the boot hook, give NickelMenu its own uninstall flag
    # only when kobodash installed it, and stay in Nickel. `uninstalled` tells the next
    # `make uninstall` run that the Kobo side is finished.
    rm -f "$R/etc/udev/rules.d/99-kobodash.rules"
    [ -e "$R/usr/local/kobodash/installed-nickelmenu" ] && touch "$R/mnt/onboard/.adds/nm/uninstall"
    rm -rf "$R/usr/local/kobodash"
    rm -f uninstall
    touch uninstalled
    echo "$(date) autostart: uninstalled, staying in Nickel" >>debug.log
    exit 0
fi

# NiLuJe's launcher runs dropbear with -n (no password check), root telnet on 23 and a writable
# FTP when these flags are missing. Keep them in place at every boot. With SSH opted in
# (authorized_keys exists; the only file NiLuJe's dropbear reads) run our own key-only dropbear.
mkdir -p "$USBNET"
touch "$USBNET/NO_SSH" "$USBNET/NO_TELNET"
if [ -s "$USBNET/authorized_keys" ] && [ ! -e "$R/tmp/kobodash-dropbear.pid" ]; then
    "$R/usr/bin/dropbear" -s -R -p 22 -P "$R/tmp/kobodash-dropbear.pid"
fi

if [ -e nickel-once ]; then
    rm -f nickel-once
    echo "$(date) autostart: power button pressed, staying in Nickel for this boot" >>debug.log
    exit 0
fi
[ -e disabled ] && exit 0
# Three boots in a row that never reach a render (trmnlloop.sh deletes bootcount) fall back to Nickel.
n=$(cat bootcount 2>/dev/null || echo 0)
if [ "$n" -ge 3 ]; then
    echo "$(date) autostart: $n boots without a render, staying in Nickel (rm bootcount to retry)" >>debug.log
    exit 0
fi
echo $((n + 1)) >bootcount

# trmnl.sh tears Nickel down itself and reuses its environment, so wait until Nickel is up.
i=0
until pkill -0 nickel || [ $i -ge 90 ]; do
    sleep 1
    i=$((i + 1))
done
sleep 15
exec /bin/sh ./trmnl.sh
