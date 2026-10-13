#!/bin/sh
# Exit 0 when the USB cable goes to a computer, not a charger. At every plug-in the kernel renames its
# plug device to usb_host for a port that talks USB (usb_plug for a charger), the name Nickel's own udev
# rule acts on. The name stays after unplugging, so the battery has to say it is on power too.
R=${KOBODASH_ROOT:-} # only the tests set this
[ -e "$R/sys/devices/platform/usb_host" ] || exit 1
case "$(cat "$R/sys/class/power_supply/mc13892_bat/status" "$R/sys/class/power_supply/battery/status" 2>/dev/null)" in
    *Charging* | *Full*) exit 0 ;;
esac
exit 1
