#!/bin/sh
# autostart.sh runs a copy of this from /tmp while Nickel has the screen for a computer: connecting hands
# the onboard partition to the computer, and a script still open on it would keep the Kobo using it too.
# Once the cable is out and the partition is back, start over at autostart.sh in $1, which also honours a
# `disabled` or `uninstall` written while connected.
computer() { /bin/sh "$(dirname "$0")/computer.sh"; }
# The kernel names the cable some seconds into boot, so give it a minute to see the computer first.
i=0
until computer || [ $i -ge 60 ]; do
    sleep 1
    i=$((i + 1))
done
while [ ! -f "$1/autostart.sh" ] || computer; do
    sleep 5
done
exec /bin/sh "$1/autostart.sh"
