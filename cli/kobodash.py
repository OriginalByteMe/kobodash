#!/usr/bin/env python3
"""Turn a Kobo e-reader into a wireless dashboard: `kobodash.py setup` or `kobodash.py uninstall`.

Add --dry-run to see every step without writing anything, --mount PATH to skip the drive list.
Standard library only, Python 3.9+. The wording is plain English on purpose: every step says what
it is doing and why.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import textwrap
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple, Optional
from urllib.parse import urlsplit

if sys.version_info < (3, 9):
    sys.exit("kobodash needs Python 3.9 or newer.")

REPO = Path(__file__).resolve().parent.parent
PORT = 8737
NEED_MB = 150  # kobo-stuff unpacks about 60 MB onto the Kobo, the update file is about 35 MB, plus backups
OVERRIDE = "I accept the risk"
KOBODASH = ".adds/kobodash"
NM_ENTRY = "menu_item :main :Start dashboard :cmd_spawn :sh /mnt/onboard/.adds/kobodash/trmnl.sh\n"
NICKEL_MENU_MARKER = "usr/local/kobodash/installed-nickelmenu"  # tells autostart.sh we installed NickelMenu


class Model(NamedTuple):
    name: str
    width: int
    height: int
    color: bool


# Model number (the last three digits of .kobo/version) -> name: FBInk's Kobo device ID list,
#   https://github.com/NiLuJe/FBInk/blob/master/fbink.h (KOBO_DEVICE_ID_E)
# Screen size: Plato's device table, https://github.com/baskerville/plato/blob/master/crates/core/src/device.rs
#   (keyed by product codename; joined to the numbers through FBInk's codenames in fbink_device_id.c).
# Tolino readers (FBInk ID + 300) are left out on purpose: they are not Kobos.
MODELS = {
    "310": Model("Kobo Touch", 600, 800, False),
    "320": Model("Kobo Touch C", 600, 800, False),
    "340": Model("Kobo Mini", 600, 800, False),
    "330": Model("Kobo Glo", 758, 1024, False),
    "371": Model("Kobo Glo HD", 1072, 1448, False),
    "372": Model("Kobo Touch 2.0", 600, 800, False),
    "360": Model("Kobo Aura", 758, 1024, False),
    "350": Model("Kobo Aura HD", 1080, 1440, False),
    "370": Model("Kobo Aura H2O", 1080, 1440, False),
    "374": Model("Kobo Aura H2O Edition 2", 1080, 1440, False),
    "378": Model("Kobo Aura H2O Edition 2 (v2)", 1080, 1440, False),
    "373": Model("Kobo Aura ONE", 1404, 1872, False),
    "381": Model("Kobo Aura ONE Limited Edition", 1404, 1872, False),
    "375": Model("Kobo Aura Edition 2", 758, 1024, False),
    "379": Model("Kobo Aura Edition 2 (v2)", 758, 1024, False),
    "376": Model("Kobo Clara HD", 1072, 1448, False),
    "377": Model("Kobo Forma", 1440, 1920, False),
    "380": Model("Kobo Forma 32GB", 1440, 1920, False),
    "384": Model("Kobo Libra H2O", 1264, 1680, False),
    "382": Model("Kobo Nia", 758, 1024, False),
    "387": Model("Kobo Elipsa", 1404, 1872, False),
    "388": Model("Kobo Libra 2", 1264, 1680, False),
    "383": Model("Kobo Sage", 1440, 1920, False),
    "386": Model("Kobo Clara 2E", 1072, 1448, False),
    "389": Model("Kobo Elipsa 2E", 1404, 1872, False),
    "390": Model("Kobo Libra Colour", 1264, 1680, True),
    "391": Model("Kobo Clara BW", 1072, 1448, False),
    "393": Model("Kobo Clara Colour", 1072, 1448, True),
    "395": Model("Kobo Clara BW", 1072, 1448, False),
}
TESTED = {"386"}  # the only model the authors have run kobodash on


class Stop(Exception):
    """A plain-English reason to stop. main() prints it and exits with 1."""


class Drive(NamedTuple):
    label: str
    size: str
    mount: Path


class Info(NamedTuple):
    firmware: str
    device_id: str
    model: Optional[Model]


# --- talking to the user -------------------------------------------------------------------------

def read_line(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Stop("\nCancelled.") from None


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "kobodash"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def guess_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # sends nothing, only picks the network interface in use
            return s.getsockname()[0]
    except OSError:
        return ""


def parse_lsblk(text: str):
    """lsblk -J output -> (mounted removable drives, removable partitions that are not mounted)."""
    drives, unmounted = [], []

    def truthy(v):
        return v in (True, 1, "1", "true")

    def walk(node, removable):
        removable = removable or truthy(node.get("rm")) or truthy(node.get("hotplug")) or node.get("tran") == "usb"
        mounts = [m for m in (node.get("mountpoints") or [node.get("mountpoint")]) if m and m != "[SWAP]"]
        if removable and mounts:
            drives.append(Drive(node.get("label") or node["name"], node.get("size") or "", Path(mounts[0])))
        elif removable and node.get("fstype") and not node.get("children"):
            unmounted.append("/dev/" + node["name"])
        for child in node.get("children") or []:
            walk(child, removable)

    for top in json.loads(text).get("blockdevices", []):
        walk(top, False)
    return drives, unmounted


def detect_drives():
    if sys.platform == "darwin":
        volumes = [p for p in sorted(Path("/Volumes").iterdir()) if p.is_dir() and not p.is_symlink()]
        return [Drive(p.name, f"{shutil.disk_usage(p).total / 1e9:.1f} GB", p) for p in volumes], []
    for mount_column in ("MOUNTPOINTS", "MOUNTPOINT"):  # older util-linux only knows the second
        cmd = ["lsblk", "-J", "-o", f"NAME,LABEL,SIZE,FSTYPE,{mount_column},RM,HOTPLUG,TRAN"]
        try:
            return parse_lsblk(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
        except (OSError, subprocess.CalledProcessError):
            continue
    return [], []


@dataclass
class Ctx:
    """Everything setup touches outside itself, so tests can stub it: input, output, network, drives."""

    repo: Path = REPO
    dry_run: bool = False
    mount: Optional[Path] = None
    pins: dict = field(default_factory=lambda: json.loads((Path(__file__).parent / "pins.json").read_text()))
    input: Callable[[str], str] = read_line
    say: Callable[[str], None] = print
    fetch: Callable[[str], bytes] = http_get
    drives: Callable[[], tuple] = detect_drives
    free_bytes: Callable[[Path], int] = lambda root: shutil.disk_usage(root).free
    lan_ip: Callable[[], str] = guess_lan_ip
    home: Path = field(default_factory=Path.home)


def ask(ctx: Ctx, prompt: str, default: str = "") -> str:
    return ctx.input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip() or default


def ask_number(ctx: Ctx, prompt: str, count: int, default: int = 0) -> int:
    while True:
        answer = ask(ctx, prompt, str(default) if default else "")
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer)
        ctx.say(f"Please type a number from 1 to {count}.")


def say_block(ctx: Ctx, text: str) -> None:
    ctx.say(textwrap.dedent(text).strip("\n"))


# --- finding and checking the Kobo ---------------------------------------------------------------

def read_info(root: Path) -> Optional[Info]:
    """.kobo/version looks like N5063AC445028,4.1.15,4.38.23828,4.1.15,4.1.15,00000000-0000-0000-0000-000000000386:
    the third field is the firmware, the last three digits are the model number."""
    try:
        parts = (root / ".kobo" / "version").read_text(errors="replace").strip().splitlines()[0].split(",")
    except (OSError, IndexError):
        return None
    if len(parts) < 3 or not re.fullmatch(r"\d{3}", parts[-1].strip()[-3:]):
        return None
    device_id = parts[-1].strip()[-3:]
    return Info(parts[2], device_id, MODELS.get(device_id))


def describe(info: Info) -> str:
    name = info.model.name if info.model else f"a Kobo I do not recognise (model number {info.device_id})"
    return f"{name}, firmware {info.firmware}"


def pick_kobo(ctx: Ctx):
    """Step 1: find the connected Kobo. Returns (folder, Info)."""
    if ctx.mount:
        root = ctx.mount
        ctx.say(f"Using the Kobo at {root}.")
    else:
        drives, unmounted = ctx.drives()
        if not drives:
            say_block(ctx, """
                I could not find a connected drive. To connect your Kobo:
                  1. Plug it into this computer with its USB cable.
                  2. On the Kobo's screen, tap "Connect" (it may say "Connect to computer").
                  3. Wait a few seconds until your computer shows the Kobo as a drive, then run this again.
                If your Kobo is already showing a dashboard, press its power button once first and wait for its normal screen.
                If your computer shows the Kobo somewhere unusual, run:  python3 cli/kobodash.py setup --mount /path/to/the/Kobo""")
            if unmounted:
                ctx.say(f"I can see a drive that is not opened yet ({', '.join(unmounted)}). Open it in your file manager first.")
            raise Stop("Nothing was changed.")
        ctx.say("Connected drives:")
        for number, drive in enumerate(drives, 1):
            info = read_info(drive.mount)
            mark = f"   <-- a Kobo: {describe(info)}" if info else ""
            ctx.say(f"  {number}. {drive.label}   {drive.size}   {drive.mount}{mark}")
        root = drives[ask_number(ctx, "Which one is your Kobo? Type its number", len(drives)) - 1].mount
    info = read_info(root)
    if not info:
        raise Stop(f"{root} is not a Kobo: it has no readable .kobo/version file. Nothing was changed.")
    ctx.say(f"Found: {describe(info)}.")
    return root, info


def check_model(ctx: Ctx, info: Info) -> Model:
    """Step 2: which model is it? Unknown ones are refused unless the user types the override phrase."""
    if info.model:
        if info.device_id not in TESTED:
            ctx.say("kobodash has only been tested on the Kobo Clara 2E. Yours should work, but it is a guess.")
        return info.model
    ctx.say(f"I do not recognise this Kobo (model number {info.device_id}), so I do not know its screen size or "
            "whether it will work.")
    ctx.say(f"To try anyway (I will assume a 1072x1448 black-and-white screen), type exactly: {OVERRIDE}")
    if ctx.input("> ").strip() != OVERRIDE:
        raise Stop("Stopped. Nothing was changed.")
    return Model("Unknown Kobo", 1072, 1448, False)


def preflight(ctx: Ctx, root: Path, info: Info) -> dict:
    """Step 3: refuse early, with a reason. Returns which other Kobo add-ons are already installed."""
    major = re.match(r"(\d+)\.", info.firmware)
    if not major or int(major.group(1)) != 4:
        raise Stop(f"This Kobo has firmware {info.firmware}. kobodash only supports 4.x firmware, because the tools it "
                   "installs are built for that version of the Kobo software. Nothing was changed.")
    if (root / ".kobo" / "KoboRoot.tgz").exists():
        raise Stop("There is already a software update waiting on this Kobo (the file .kobo/KoboRoot.tgz). A Kobo installs "
                   "only one at a time, so I will not replace it. Eject the Kobo, let it restart and finish, then run "
                   "this again. Nothing was changed.")
    if not os.access(root, os.W_OK):
        raise Stop(f"I cannot write to {root} (it is read-only). Nothing was changed.")
    free_mb = ctx.free_bytes(root) // 2**20
    if free_mb < NEED_MB:
        raise Stop(f"The Kobo has only {free_mb} MB free and I need about {NEED_MB} MB (the tools take about 60 MB, plus "
                   "the update file itself). Remove a few books, then run this again. Nothing was changed.")
    found = {key: (root / path).exists() for key, path in
             (("nm", ".adds/nm"), ("stuff", ".niluje"), ("trmnl", ".adds/TRMNL"), ("koreader", ".adds/koreader"))}
    notes = {
        "nm": "NickelMenu is already installed. It stays as it is; I only add one menu entry.",
        "stuff": "NiLuJe's kobo-stuff is already installed. It stays as it is and is not installed again.",
        "trmnl": "A TRMNL client is already installed (.adds/TRMNL). It is left in place; kobodash uses its own copy.",
        "koreader": "KOReader is installed. It is left alone.",
    }
    for key, present in found.items():
        if present:
            ctx.say(f"  Already on your Kobo: {notes[key]}")
    return found


def confirm_changes(ctx: Ctx, info: Info, found: dict) -> None:
    """Step 4: say exactly what will change, then require a typed `yes`."""
    items = []
    if not found["nm"]:
        items.append('Install NickelMenu. It adds a small menu to your Kobo, with a "Start dashboard" entry.')
    if not found["stuff"]:
        items.append("Install NiLuJe's kobo-stuff tools: small helper programs the dashboard needs.")
    items += [
        "Install a small startup hook, so that after it starts up your Kobo shows the dashboard instead of the reading screen.",
        f"Copy the dashboard program into a new folder on the Kobo, {KOBODASH}.",
        "Turn off the open servers kobo-stuff would otherwise start (SSH without a password, telnet and FTP: anyone on "
        "your Wi-Fi could get in). SSH stays off unless you turn it on below, and then it accepts only your key.",
    ]
    ctx.say(f"\nHere is exactly what will change on your {describe(info)}:\n")
    for number, item in enumerate(items, 1):
        ctx.say(textwrap.fill(f"{number}. {item}", 100, subsequent_indent="   "))
    say_block(ctx, f"""
        Your books, reading settings and Kobo account are not touched. Any file I replace is first copied to
        {KOBODASH}/backup/.

        After you eject and unplug it, the Kobo installs the next time it restarts (about a minute). After that it
        shows the dashboard and is no longer a normal e-reader. To get back to the normal Kobo:
          - Press its power button while it is asleep and not charging. It restarts into the normal Kobo, once.
            (If nothing happens within a minute, press it again. Plug it in and tap Connect to work on it.)
          - Remove kobodash for good: connect the Kobo and run  make uninstall
          - Or keep it normal: create an empty file named "disabled" in the {KOBODASH} folder on the Kobo.
        """)
    if ctx.input("Type yes to go ahead, or anything else to stop: ").strip() != "yes":
        raise Stop("Stopped. Nothing was changed.")


# --- the questions -------------------------------------------------------------------------------

@dataclass
class Settings:
    name: str
    base: str  # http://host:port, no trailing slash
    lat: str
    lon: str
    dashboard: str
    ssh_key: str  # the public key line, or "" for no SSH


def server_base(text: str) -> str:
    """'192.168.1.50', '192.168.1.50:8737' or 'http://host/api' -> 'http://192.168.1.50:8737' ('' if unusable)."""
    text = text.strip().rstrip("/")
    text = text[:-4] if text.endswith("/api") else text
    parts = urlsplit(text if "://" in text else "http://" + text)
    try:
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    netloc = parts.netloc if port or parts.scheme == "https" else f"{parts.netloc}:{PORT}"
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"


def list_dashboards(repo: Path):
    found = []
    for path in sorted((repo / "dashboards").glob("*/dashboard.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found.append((path.parent.name, str(data.get("name", path.parent.name)), str(data.get("description", ""))))
    return found


def ask_coordinate(ctx: Ctx, prompt: str, limit: int) -> str:
    while True:
        answer = ask(ctx, prompt)
        try:
            if not answer or abs(float(answer)) <= limit:
                return answer
        except ValueError:
            pass
        ctx.say(f"That is not a number between -{limit} and {limit}.")


def ask_ssh_key(ctx: Ctx) -> str:
    if ask(ctx, "Turn on SSH, so you can log in to the Kobo from this computer with a key? (not needed) [y/N]").lower() not in ("y", "yes"):
        return ""
    keys = sorted((ctx.home / ".ssh").glob("*.pub"))
    if not keys:
        ctx.say("I found no public key in ~/.ssh (create one with: ssh-keygen -t ed25519). Continuing without SSH.")
        return ""
    for number, key in enumerate(keys, 1):
        ctx.say(f"  {number}. {key.name}")
    chosen = keys[ask_number(ctx, "Which key may log in? Type its number", len(keys), 1) - 1]
    text = chosen.read_text().strip()
    line = text.splitlines()[0] if text else ""
    if not re.match(r"(ssh-|ecdsa-|sk-)\S+ \S+", line):
        raise Stop(f"{chosen} does not look like an SSH public key. Nothing was changed.")
    return line


def ask_settings(ctx: Ctx) -> Settings:
    """Step 5."""
    ctx.say("\nA few questions. Press Enter to accept the suggestion in [brackets].\n")
    ctx.say("The dashboard server is a small program (started with `make serve`) that draws the picture the Kobo shows.")
    while True:
        lan = ctx.lan_ip()
        base = server_base(ask(ctx, "Address of the computer that will run it", f"{lan}:{PORT}" if lan else ""))
        if base:
            break
        ctx.say("That does not look like an address, for example 192.168.1.50:8737.")
    while True:
        name = ask(ctx, "A name for this Kobo", "My Kobo")
        if re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", name):
            break
        ctx.say("Please use only letters, numbers, spaces, dots, dashes and underscores (40 at most).")
    ctx.say("Your location lets the dashboard show the weather. You can skip it and add LAT and LON to .env later.")
    lat = ask_coordinate(ctx, "Latitude, like 51.5 (optional)", 90)
    lon = ask_coordinate(ctx, "Longitude, like -0.12", 180) if lat else ""
    dashboards = list_dashboards(ctx.repo)
    if dashboards:
        ctx.say("Dashboards you can show:")
        for number, (folder, title, text) in enumerate(dashboards, 1):
            ctx.say(f"  {number}. {title} ({folder}): {text}")
        default = next((n for n, d in enumerate(dashboards, 1) if d[0] == "home"), 1)
        dashboard = dashboards[ask_number(ctx, "Which one? Type its number", len(dashboards), default) - 1][0]
    else:
        ctx.say("I found no dashboards in the dashboards folder, so the .env will point at one called home.")
        dashboard = "home"
    return Settings(name, base, lat, lon, dashboard, ask_ssh_key(ctx))


# --- downloads -----------------------------------------------------------------------------------

def download(ctx: Ctx, path: str, sha256: str, what: str) -> bytes:
    """Fetch a pinned file from the upstream repo (cached by checksum). Aborts on any mismatch."""
    cached = ctx.repo / ".cache" / "downloads" / sha256
    if cached.is_file():
        data = cached.read_bytes()
        if hashlib.sha256(data).hexdigest() == sha256:
            return data
    url = f"https://raw.githubusercontent.com/{ctx.pins['repo']}/{ctx.pins['commit']}/{path}"
    ctx.say(f"  downloading {what}")
    try:
        data = ctx.fetch(url)
    except (OSError, ValueError, http.client.HTTPException) as error:  # HTTPException: a cut-off download
        raise Stop(f"I could not download {what} ({error}). Check your internet connection and try again. "
                   "Nothing was written to your Kobo.") from None
    if hashlib.sha256(data).hexdigest() != sha256:
        raise Stop(f"The downloaded {what} does not match the checksum this version of kobodash expects, so I will not "
                   "use it. Nothing was written to your Kobo.")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data


def fetch_pinned(ctx: Ctx, found: dict):
    """Step 6: everything from upstream, checksum-verified, before the Kobo is touched.
    Returns (NickelMenu update or None, kobo-stuff update or None, dashboard client files)."""
    ctx.say("\nDownloading the pieces I need and checking each against its fingerprint (sha256):")
    pins = ctx.pins
    nm = None if found["nm"] else download(ctx, pins["nickelmenu"]["path"], pins["nickelmenu"]["sha256"], "NickelMenu")
    stuff = None
    if not found["stuff"]:
        archive = download(ctx, pins["kobostuff"]["path"], pins["kobostuff"]["sha256"], "NiLuJe's kobo-stuff (about 30 MB)")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:xz") as tar:
            stuff = tar.extractfile(pins["kobostuff"]["member"]).read()
    client = {rel: download(ctx, pins["client_dir"] + rel, sha, f"dashboard client file {rel}")
              for rel, sha in pins["client"].items()}
    ctx.say("  everything matches")
    return nm, stuff, client


# --- building what goes on the Kobo --------------------------------------------------------------

def tree_files(base: Path):
    return {p.relative_to(base).as_posix(): p for p in sorted(base.rglob("*")) if p.is_file()}


def build_root_tgz(out: Path, updates: list, rootfs: Path, extra: dict) -> int:
    """One .kobo/KoboRoot.tgz (a Kobo installs only one per boot): the upstream updates, then device/rootfs,
    then `extra` {path: (bytes, mode)}. Later entries win. Returns the number of entries."""
    count = 0

    def add(tar, name, data, mode):
        info = tarfile.TarInfo("./" + name)
        info.size, info.mode, info.mtime = len(data), mode, int(time.time())
        info.uname = info.gname = "root"
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(out, "w:gz", compresslevel=6, format=tarfile.GNU_FORMAT) as tar:  # the Kobo's old tar reads GNU
        for blob in updates:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as source:
                for member in source:
                    tar.addfile(member, source.extractfile(member) if member.isreg() else None)
                    count += 1
        for name, path in tree_files(rootfs).items():
            add(tar, name, path.read_bytes(), 0o755 if os.access(path, os.X_OK) else 0o644)
            count += 1
        for name, (data, mode) in extra.items():
            add(tar, name, data, mode)
            count += 1
    return count


def new_secrets() -> dict:
    return {"token": secrets.token_urlsafe(24), "ota_key": secrets.token_hex(32)}


def config_json(s: Settings, token: str, ota_key: str) -> bytes:
    config = {"TrmnlId": s.name, "TrmnlToken": token, "TrmnlApiUrl": s.base + "/api", "OtaKey": ota_key,
              "LoopMaxIteration": 0, "DebugToScreen": 0, "ConnectedGracePeriod": 15, "ImageFormat": "png"}
    return (json.dumps(config, indent=4) + "\n").encode()


def local_timezone() -> str:
    match = re.search(r"zoneinfo/(.+)$", os.path.realpath("/etc/localtime"))
    return os.environ.get("TZ", "").lstrip(":") or (match.group(1) if match else "UTC")


def env_text(s: Settings, model: Model, token: str, ota_key: str) -> str:
    return (f"KOBODASH_TOKEN={token}\nOTA_KEY={ota_key}\nDASHBOARD={s.dashboard}\nPUBLIC_URL={s.base}\n"
            f"SCREEN={model.width}x{model.height}\nCOLOR={int(model.color)}\nTZ={local_timezone()}\nLAT={s.lat}\nLON={s.lon}\n")


class KoboFiles:
    """Writes to the Kobo. Replaced files are backed up first; in a dry run it only says what it would do."""

    def __init__(self, ctx: Ctx, root: Path):
        self.ctx, self.root = ctx, root
        self.stamp = time.strftime("%Y%m%d-%H%M%S")

    def put(self, rel: str, data: bytes) -> None:
        dest = self.root / rel
        if dest.is_file():
            if dest.read_bytes() == data:
                return
            copy = f"{KOBODASH}/backup/{self.stamp}/{rel}"
            self.ctx.say(f"  {'would back up' if self.ctx.dry_run else 'backing up'} {rel} to {copy}")
            if not self.ctx.dry_run:
                (self.root / copy).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(dest, self.root / copy)
        if self.ctx.dry_run:
            self.ctx.say(f"  would write {rel}")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def put_file(self, rel: str, source: Path) -> None:
        dest = self.root / rel
        if self.ctx.dry_run:
            self.ctx.say(f"  would write {rel} ({source.stat().st_size / 2**20:.0f} MB)")
            return
        partial = dest.with_name(dest.name + ".partial")  # the Kobo only acts on the exact name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, partial)
        os.replace(partial, dest)


def write_env(ctx: Ctx, text: str) -> None:
    """Step 9: .env for the server. Printed too, for servers that run on another machine."""
    ctx.say("\nThe server reads these settings from the file .env in this folder. If it runs on another machine, "
            "put these lines in its .env instead:\n")
    ctx.say(textwrap.indent(text, "    "))
    path = ctx.repo / ".env"
    if ctx.dry_run:
        ctx.say(f"  would write {path}" + (" (after asking, and keeping a copy of the old one)" if path.exists() else ""))
        return
    if path.exists():
        if ask(ctx, f"{path} already exists. Replace it? (A copy is kept in .cache/) [y/N]").lower() not in ("y", "yes"):
            ctx.say("Left your .env alone. Copy the lines above into it so the server matches this Kobo.")
            return
        backup = ctx.repo / ".cache" / f"env-backup-{time.strftime('%Y%m%d-%H%M%S')}"  # .cache is git-ignored
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, backup)
    path.write_text(text)
    os.chmod(path, 0o600)  # it holds the Kobo's token
    ctx.say(f"Wrote {path}")


# --- setup ---------------------------------------------------------------------------------------

def setup(ctx: Ctx) -> None:
    root, info = pick_kobo(ctx)
    model = check_model(ctx, info)
    found = preflight(ctx, root, info)
    confirm_changes(ctx, info, found)
    settings = ask_settings(ctx)
    nm, stuff, client = fetch_pinned(ctx, found)

    keys = new_secrets()
    client.update({rel: path.read_bytes() for rel, path in tree_files(ctx.repo / "device" / "client").items()})
    client["config.json"] = config_json(settings, keys["token"], keys["ota_key"])
    extra = {}
    if nm:
        extra[NICKEL_MENU_MARKER] = (b"", 0o644)
    if settings.ssh_key:
        extra["usr/local/niluje/usbnet/etc/authorized_keys"] = ((settings.ssh_key + "\n").encode(), 0o600)
    built = ctx.repo / ".cache" / "KoboRoot.tgz"
    built.parent.mkdir(parents=True, exist_ok=True)
    entries = build_root_tgz(built, [b for b in (nm, stuff) if b], ctx.repo / "device" / "rootfs", extra)
    ctx.say(f"  built the update file: {entries} entries, {built.stat().st_size / 2**20:.0f} MB")

    ctx.say("\nWriting to the Kobo" + (" (dry run: nothing is really written)" if ctx.dry_run else "") + ":")
    kobo = KoboFiles(ctx, root)
    ctx.say(f"  the dashboard program, in {KOBODASH} ({len(client)} files)")
    for rel, data in client.items():
        kobo.put(f"{KOBODASH}/{rel}", data)
    kobo.put(".adds/nm/kobodash", NM_ENTRY.encode())
    ctx.say("  the update that installs the startup hook" + (", NickelMenu" if nm else "") + (", kobo-stuff" if stuff else ""))
    kobo.put_file(".kobo/KoboRoot.tgz", built)
    if not ctx.dry_run and hasattr(os, "sync"):
        os.sync()

    write_env(ctx, env_text(settings, model, keys["token"], keys["ota_key"]))
    if ctx.dry_run:
        ctx.say("\nDry run finished: nothing was written to your Kobo or to .env.")
        return
    say_block(ctx, """

        All done on the Kobo's side. Three steps left:
          1. Eject the Kobo safely (in your file manager, or Eject in Finder), then unplug the cable.
          2. The Kobo should restart and install by itself. If it is still on its home screen after a minute,
             restart it: hold the power button, choose Power off, then switch it on again.
          3. Start the dashboard server:  make serve
             Until it is running the Kobo shows an error picture. That is fine.
        """)
    if settings.ssh_key:
        ctx.say("SSH is on: log in as root with your key while the Kobo is awake, which is whenever it is charging.")


# --- uninstall -----------------------------------------------------------------------------------

def uninstall(ctx: Ctx) -> None:
    root, info = pick_kobo(ctx)
    folder = root / KOBODASH
    menu_entry = root / ".adds" / "nm" / "kobodash"
    if (folder / "uninstalled").exists():
        ctx.say(f"The Kobo has finished removing the startup hook. What is left is the folder {KOBODASH}"
                " (it also holds the backups setup made) and the menu entry .adds/nm/kobodash.")
        if ctx.input("Type yes to delete them, or anything else to keep them: ").strip() == "yes":
            if ctx.dry_run:
                ctx.say(f"  would delete {folder} and {menu_entry}")
            else:
                shutil.rmtree(folder, ignore_errors=True)
                menu_entry.unlink(missing_ok=True)
                ctx.say("Deleted. NiLuJe's kobo-stuff is left installed; it is harmless now that its servers are off.")
        return
    if not folder.is_dir():
        ctx.say("kobodash is not installed on this Kobo. Nothing to do.")
        return
    if (folder / "uninstall").exists():
        ctx.say("The removal is already prepared. Eject the Kobo and let it restart, then run this again.")
        return
    if (root / ".kobo" / "KoboRoot.tgz").exists():
        raise Stop("There is already a software update waiting on this Kobo (.kobo/KoboRoot.tgz). Eject the Kobo, "
                   "let it restart and finish, then run this again. Nothing was changed.")
    say_block(ctx, f"""
        This removes kobodash from your {describe(info)}: the next time it starts, it removes the startup hook that
        shows the dashboard and stays on its normal screen. If kobodash installed NickelMenu, that is removed too
        (its menu may need one more restart to disappear). Your books and settings are not touched, and NiLuJe's
        kobo-stuff stays installed.
        """)
    if ctx.input("Type yes to go ahead, or anything else to stop: ").strip() != "yes":
        raise Stop("Stopped. Nothing was changed.")
    ctx.say("Writing the removal request" + (" (dry run: nothing is really written)" if ctx.dry_run else "") + ":")
    kobo = KoboFiles(ctx, root)
    kobo.put(f"{KOBODASH}/uninstall", b"")
    # An update file makes the Kobo restart when it is ejected, if it does that on its own. It only holds an empty folder.
    trigger = ctx.repo / ".cache" / "restart.tgz"
    trigger.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(trigger, "w:gz") as tar:
        folder_entry = tarfile.TarInfo("./usr/local/kobodash")
        folder_entry.type, folder_entry.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(folder_entry)
    kobo.put_file(".kobo/KoboRoot.tgz", trigger)
    if not ctx.dry_run and hasattr(os, "sync"):
        os.sync()
    say_block(ctx, """
        Done. Now:
          1. Eject the Kobo safely and unplug it. It restarts by itself and removes the startup hook.
             (If it does not restart within a minute, switch it off and on again.)
          2. When it shows its normal screen again, connect it once more and run  make uninstall  a second time
             to delete the leftover files.
        """)


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="show every step but write nothing to the Kobo or to .env")
    common.add_argument("--mount", type=Path, metavar="PATH", help="the Kobo's folder; skips the list of drives")
    parser = argparse.ArgumentParser(prog="kobodash", description="Turn a Kobo e-reader into a wireless dashboard.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", parents=[common], help="install kobodash on a connected Kobo")
    commands.add_parser("uninstall", parents=[common], help="remove kobodash from a connected Kobo")
    args = parser.parse_args(argv)
    ctx = Ctx(dry_run=args.dry_run, mount=args.mount)
    try:
        {"setup": setup, "uninstall": uninstall}[args.command](ctx)
    except Stop as stop:
        print(stop)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
