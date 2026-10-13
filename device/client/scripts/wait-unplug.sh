#!/bin/sh
# autostart.sh runs a copy of this from /tmp while Nickel has the screen for a computer: connecting hands
# the onboard partition to the computer, and a script still open on it would keep the Kobo using it too.
# Once the cable is out and the partition is back, start over at autostart.sh in $1, which also honours a
# `disabled` or `uninstall` written while connected.
while [ ! -f "$1/autostart.sh" ] || /bin/sh "$(dirname "$0")/computer.sh"; do
    sleep 5
done
exec /bin/sh "$1/autostart.sh"
