#!/bin/sh
# Installed as /usr/local/kobodash/boot.sh; udev runs it early at every boot through
# /etc/udev/rules.d/99-kobodash.rules. Detach so udev does not kill it, wait for the
# onboard partition, then run the dashboard's autostart.sh (which updates over the air).
[ "$1" = detached ] || { env -i -- setsid /bin/sh "$0" detached & exit 0; }
export PATH=/usr/local/niluje/usbnet/bin:/sbin:/usr/sbin:/bin:/usr/bin
i=0
while [ $i -lt 120 ]; do
    [ -f /mnt/onboard/.adds/kobodash/autostart.sh ] && exec /bin/sh /mnt/onboard/.adds/kobodash/autostart.sh
    sleep 1
    i=$((i + 1))
done
