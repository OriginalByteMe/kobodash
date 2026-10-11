#!/bin/sh
# Over-the-air client update. $1: the /api/display answer (firmware_url, firmware_hmac).
# Installs over the client directory only a package signed with this Kobo's OtaKey
# (config.json; never sent over the wire) and whose scripts all parse. config.json and
# bin/ are not in the package and stay as they are. Exit 0 means installed: reboot into it.
key=$(jq -r '.OtaKey // empty' config.json)
[ -n "$key" ] || { echo "update: no OtaKey in config.json, refusing"; exit 1; }
url=$(jq -r '.firmware_url' "$1")
mac=$(jq -r '.firmware_hmac' "$1")
rm -rf /tmp/client.tgz /tmp/client.new && mkdir -p /tmp/client.new || exit 1
curl -fsSL -o /tmp/client.tgz "$url" || { echo "update: download failed"; exit 1; }
[ "$(openssl dgst -sha256 -hmac "$key" -r /tmp/client.tgz | cut -d' ' -f1)" = "$mac" ] || { echo "update: bad signature"; exit 1; }
tar -xzf /tmp/client.tgz -C /tmp/client.new || { echo "update: bad archive"; exit 1; }
for f in $(find /tmp/client.new -name '*.sh'); do
    sh -n "$f" || { echo "update: $f does not parse"; exit 1; }
done
cp -r /tmp/client.new/. . && sync || { echo "update: copy failed"; exit 1; }
echo "update: installed client $(cat version.txt)"
