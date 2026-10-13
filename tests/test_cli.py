"""Tests for cli/kobodash.py and the device scripts. Standard library only; the network is stubbed and the
"Kobo" is a temp folder with a .kobo/version file. Run: python3 -m unittest tests/test_cli.py"""
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("kobodash_cli", ROOT / "cli" / "kobodash.py")
kd = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = kd
_spec.loader.exec_module(kd)

CLARA_2E = "N5063AC445028,4.1.15,4.38.23828,4.1.15,4.1.15,00000000-0000-0000-0000-000000000386\n"
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample me@example.com"
# what a user types in a usual run: yes | server address | name | latitude (skipped) | dashboard (default) | no SSH
ANSWERS = ["yes", "10.0.0.5", "Kitchen", "", "", "n"]


def tgz(files, links=None):
    """A tar.gz in memory: files {name: bytes}, links {name: target}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return buf.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Upstream:
    """Tiny stand-ins for the three pinned upstream downloads, plus the pins that describe them."""

    def __init__(self):
        self.nm = tgz({"./mnt/onboard/.adds/nm/doc": b"doc", "./usr/local/Kobo/imageformats/libnm.so": b"nm"})
        stuff_root = tgz({"./usr/local/niluje/usbnet/bin/dropbearmulti": b"db"}, {"./usr/bin/dropbear": "x"})
        xz = io.BytesIO()
        with tarfile.open(fileobj=xz, mode="w:xz") as tar:
            info = tarfile.TarInfo("KoboStuff/KoboRoot.tgz")
            info.size = len(stuff_root)
            tar.addfile(info, io.BytesIO(stuff_root))
        self.stuff = xz.getvalue()
        self.client = {"trmnl.sh": b"#!/bin/sh\n", "trmnlloop.sh": b"#!/bin/sh\n# upstream\n", "config.json": b"{}",
                       "bin/fbink/fbink": b"\x7fELF"}
        self.pins = {
            "repo": "usetrmnl/trmnl-kobo", "commit": "c" * 40, "client_dir": "src/TRMNL/",
            "nickelmenu": {"path": "doc/nm.tgz", "sha256": sha(self.nm)},
            "kobostuff": {"path": "doc/stuff.tar.xz", "sha256": sha(self.stuff), "member": "KoboStuff/KoboRoot.tgz"},
            "client": {rel: sha(data) for rel, data in self.client.items()},
        }
        base = "https://raw.githubusercontent.com/usetrmnl/trmnl-kobo/" + "c" * 40 + "/"
        self.urls = {base + "doc/nm.tgz": self.nm, base + "doc/stuff.tar.xz": self.stuff}
        self.urls.update({base + "src/TRMNL/" + rel: data for rel, data in self.client.items()})
        self.requested = []

    def fetch(self, url):
        self.requested.append(url)
        if url not in self.urls:
            raise OSError("offline")
        return self.urls[url]


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): (p.read_bytes() if p.is_file() else None) for p in sorted(folder.rglob("*"))}


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        shutil.copytree(ROOT / "device", self.repo / "device")
        for folder, title in (("home", "Home"), ("weather", "Weather")):
            (self.repo / "dashboards" / folder).mkdir(parents=True)
            (self.repo / "dashboards" / folder / "dashboard.json").write_text(json.dumps({"name": title, "description": "x"}))
        self.kobo = self.tmp / "KOBOeReader"
        self.make_kobo(self.kobo)
        self.home = self.tmp / "home"
        (self.home / ".ssh").mkdir(parents=True)
        self.upstream = Upstream()
        self.out = []

    def make_kobo(self, folder, version=CLARA_2E):
        (folder / ".kobo").mkdir(parents=True)
        (folder / ".kobo" / "version").write_text(version)
        (folder / "My Book.epub").write_bytes(b"book")

    def run_cli(self, answers, command="setup", dry_run=False, mount=True, **overrides):
        answers = list(answers)

        def ask(prompt):
            self.assertTrue(answers, f"unexpected extra question: {prompt!r}")
            return answers.pop(0)

        settings = dict(repo=self.repo, dry_run=dry_run, mount=self.kobo if mount else None, pins=self.upstream.pins,
                        input=ask, say=self.out.append, fetch=self.upstream.fetch, free_bytes=lambda root: 10**9,
                        lan_ip=lambda: "192.168.1.50", home=self.home)
        settings.update(overrides)
        {"setup": kd.setup, "uninstall": kd.uninstall}[command](kd.Ctx(**settings))
        self.assertEqual(answers, [], "some answers were never asked for")

    def said(self):
        return "\n".join(self.out)

    def root_tgz(self):
        return tarfile.open(self.kobo / ".kobo" / "KoboRoot.tgz")


class Refusals(CliCase):
    def test_refuses_a_drive_that_is_not_a_kobo(self):
        usb = self.tmp / "USBSTICK"
        usb.mkdir()
        (usb / "photo.jpg").write_bytes(b"x")
        before = snapshot(usb)
        drives = lambda: ([kd.Drive("USBSTICK", "14.9G", usb), kd.Drive("KOBOeReader", "29.8G", self.kobo)], [])
        with self.assertRaises(kd.Stop) as stop:
            self.run_cli(["1"], mount=False, drives=drives)
        self.assertIn("not a Kobo", str(stop.exception))
        self.assertEqual(snapshot(usb), before)
        self.assertIn("a Kobo: Kobo Clara 2E, firmware 4.38.23828", self.said())  # the list marks the real one

    def test_refuses_no_drives_and_says_how_to_connect(self):
        with self.assertRaises(kd.Stop):
            self.run_cli([], mount=False, drives=lambda: ([], []))
        self.assertIn('tap "Connect"', self.said())

    def test_unknown_model_needs_the_override_phrase(self):
        (self.kobo / ".kobo" / "version").write_text(CLARA_2E.replace("386", "999"))
        before = snapshot(self.kobo)
        with self.assertRaises(kd.Stop):
            self.run_cli(["no thanks"])
        self.assertEqual(snapshot(self.kobo), before)
        self.run_cli([kd.OVERRIDE] + ANSWERS)
        self.assertTrue((self.kobo / ".kobo" / "KoboRoot.tgz").exists())

    def test_refuses_when_an_update_is_already_waiting(self):
        (self.kobo / ".kobo" / "KoboRoot.tgz").write_bytes(b"someone else's update")
        before = snapshot(self.kobo)
        with self.assertRaises(kd.Stop) as stop:
            self.run_cli([])
        self.assertIn("already a software update waiting", str(stop.exception))
        self.assertEqual(snapshot(self.kobo), before)

    def test_refuses_firmware_outside_4x_and_too_little_space(self):
        for label, version, free, message in (
            ("firmware 5", CLARA_2E.replace("4.38.23828", "5.1.0"), 10**9, "4.x"),
            ("firmware 3", CLARA_2E.replace("4.38.23828", "3.19.5761"), 10**9, "4.x"),
            ("no space", CLARA_2E, 10 * 2**20, "10 MB free"),
        ):
            with self.subTest(label):
                (self.kobo / ".kobo" / "version").write_text(version)
                with self.assertRaises(kd.Stop) as stop:
                    self.run_cli([], free_bytes=lambda root, free=free: free)
                self.assertIn(message, str(stop.exception))

    def test_checksum_mismatch_aborts_with_nothing_written(self):
        up = self.upstream
        base = "https://raw.githubusercontent.com/usetrmnl/trmnl-kobo/" + "c" * 40 + "/"
        for label, url in (("NickelMenu", base + "doc/nm.tgz"), ("kobo-stuff", base + "doc/stuff.tar.xz"),
                           ("last client file", base + "src/TRMNL/trmnl.sh")):
            with self.subTest(label):
                before = snapshot(self.kobo)
                saved = up.urls[url]
                up.urls[url] = saved + b"tampered"
                with self.assertRaises(kd.Stop) as stop:
                    self.run_cli(ANSWERS)
                self.assertIn("checksum", str(stop.exception))
                self.assertIn("Nothing was written to your Kobo", str(stop.exception))
                self.assertEqual(snapshot(self.kobo), before)
                shutil.rmtree(self.repo / ".cache", ignore_errors=True)  # a bad download must never be cached
                up.urls[url] = saved


class Setup(CliCase):
    def test_dry_run_writes_nothing_to_the_kobo_or_env(self):
        before = snapshot(self.kobo)
        drives = lambda: ([kd.Drive("KOBOeReader", "29.8G", self.kobo)], [])
        self.run_cli(["1"] + ANSWERS, dry_run=True, mount=False, drives=drives)
        self.assertEqual(snapshot(self.kobo), before)
        self.assertFalse((self.repo / ".env").exists())
        self.assertIn("would write .kobo/KoboRoot.tgz", self.said())
        self.assertIn("would write .adds/kobodash/config.json", self.said())
        self.assertIn("Dry run finished", self.said())

    def test_root_tgz_has_everything_and_no_ssh_key_by_default(self):
        self.run_cli(ANSWERS)
        with self.root_tgz() as tar:
            names = {m.name.lstrip("./") for m in tar}
            modes = {m.name.lstrip("./"): m.mode for m in tar}
            self.assertEqual(tar.getmember("./usr/local/niluje/usbnet/etc/NO_SSH").size, 0)
        for expected in ("usr/local/Kobo/imageformats/libnm.so",          # NickelMenu
                         "usr/local/niluje/usbnet/bin/dropbearmulti",       # kobo-stuff
                         "usr/bin/dropbear",
                         "usr/local/kobodash/boot.sh",                      # the startup hook
                         "etc/udev/rules.d/99-kobodash.rules",
                         "usr/local/niluje/usbnet/etc/NO_SSH",
                         "usr/local/niluje/usbnet/etc/NO_TELNET",
                         "usr/local/kobodash/installed-nickelmenu"):
            self.assertIn(expected, names)
        self.assertNotIn("usr/local/niluje/usbnet/etc/authorized_keys", names)
        self.assertEqual(modes["usr/local/kobodash/boot.sh"], 0o755)

    def test_ssh_key_is_added_only_when_opted_in(self):
        (self.home / ".ssh" / "id_ed25519.pub").write_text(PUBKEY + "\n")
        self.run_cli(["yes", "10.0.0.5", "Kitchen", "", "", "y", "1"])
        with self.root_tgz() as tar:
            member = tar.getmember("./usr/local/niluje/usbnet/etc/authorized_keys")
            self.assertEqual(member.mode, 0o600)
            self.assertEqual(tar.extractfile(member).read(), (PUBKEY + "\n").encode())

    def test_config_json_and_env_agree(self):
        self.run_cli(["yes", "10.0.0.5", "Kitchen", "51.5", "-0.12", "", "n"])
        config = json.loads((self.kobo / ".adds" / "kobodash" / "config.json").read_text())
        self.assertEqual(set(config), {"TrmnlId", "TrmnlToken", "TrmnlApiUrl", "OtaKey", "LoopMaxIteration",
                                       "DebugToScreen", "ConnectedGracePeriod", "ImageFormat"})
        self.assertEqual(config["TrmnlId"], "Kitchen")
        self.assertEqual(config["TrmnlApiUrl"], "http://10.0.0.5:8737/api")
        self.assertEqual((config["LoopMaxIteration"], config["DebugToScreen"], config["ConnectedGracePeriod"],
                          config["ImageFormat"]), (0, 0, 15, "png"))
        self.assertRegex(config["OtaKey"], r"^[0-9a-f]{64}$")
        env = dict(line.split("=", 1) for line in (self.repo / ".env").read_text().splitlines())
        self.assertEqual(env["KOBODASH_TOKEN"], config["TrmnlToken"])
        self.assertEqual(env["OTA_KEY"], config["OtaKey"])
        self.assertEqual((env["PUBLIC_URL"], env["DASHBOARD"], env["SCREEN"], env["COLOR"], env["LAT"], env["LON"]),
                         ("http://10.0.0.5:8737", "home", "1072x1448", "0", "51.5", "-0.12"))
        self.assertEqual(stat.S_IMODE((self.repo / ".env").stat().st_mode), 0o600)

    def test_client_is_upstream_plus_our_overlay_and_menu_entry(self):
        self.run_cli(ANSWERS)
        client = self.kobo / ".adds" / "kobodash"
        self.assertEqual((client / "bin" / "fbink" / "fbink").read_bytes(), b"\x7fELF")           # upstream file
        self.assertEqual((client / "trmnlloop.sh").read_bytes(), (ROOT / "device/client/trmnlloop.sh").read_bytes())
        self.assertTrue((client / "autostart.sh").is_file())
        self.assertTrue((client / "LICENSE.trmnl").is_file())
        self.assertIn("/mnt/onboard/.adds/kobodash/trmnl.sh", (self.kobo / ".adds" / "nm" / "kobodash").read_text())
        self.assertEqual((self.kobo / "My Book.epub").read_bytes(), b"book")

    def test_replaced_files_are_backed_up(self):
        (self.kobo / ".adds" / "kobodash").mkdir(parents=True)
        (self.kobo / ".adds" / "kobodash" / "config.json").write_text("old config")
        (self.kobo / ".adds" / "nm").mkdir(parents=True)
        (self.kobo / ".adds" / "nm" / "kobodash").write_text("old menu entry")
        self.run_cli(ANSWERS)
        backups = list((self.kobo / ".adds" / "kobodash" / "backup").glob("*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / ".adds/kobodash/config.json").read_text(), "old config")
        self.assertEqual((backups[0] / ".adds/nm/kobodash").read_text(), "old menu entry")
        self.assertNotEqual((self.kobo / ".adds/kobodash/config.json").read_text(), "old config")

    def test_existing_env_is_only_replaced_when_the_user_agrees(self):
        (self.repo / ".env").write_text("OLD=1\n")
        self.run_cli(ANSWERS + ["n"])
        self.assertEqual((self.repo / ".env").read_text(), "OLD=1\n")
        self.assertIn("KOBODASH_TOKEN=", self.said())  # still printed, to copy by hand
        (self.kobo / ".kobo" / "KoboRoot.tgz").unlink()  # the Kobo installed it
        self.run_cli(ANSWERS + ["y"])
        self.assertIn("KOBODASH_TOKEN=", (self.repo / ".env").read_text())
        self.assertEqual([p.read_text() for p in (self.repo / ".cache").glob("env-backup-*")], ["OLD=1\n"])

    def test_addons_already_on_the_kobo_are_left_in_place(self):
        (self.kobo / ".adds" / "nm").mkdir(parents=True)
        (self.kobo / ".niluje").mkdir()
        self.run_cli(ANSWERS)
        with self.root_tgz() as tar:
            names = {m.name.lstrip("./") for m in tar}
        self.assertNotIn("usr/local/Kobo/imageformats/libnm.so", names)
        self.assertNotIn("usr/bin/dropbear", names)
        self.assertNotIn("usr/local/kobodash/installed-nickelmenu", names)  # so uninstall will not remove their NickelMenu
        self.assertIn("usr/local/niluje/usbnet/etc/NO_SSH", names)
        self.assertFalse([u for u in self.upstream.requested if u.endswith(("nm.tgz", "stuff.tar.xz"))])
        self.assertIn("Already on your Kobo: NickelMenu", self.said())


class Uninstall(CliCase):
    def test_two_step_uninstall(self):
        self.run_cli(ANSWERS)
        (self.kobo / ".kobo" / "KoboRoot.tgz").unlink()  # the Kobo installed it
        self.run_cli(["yes"], "uninstall")
        self.assertTrue((self.kobo / ".adds/kobodash/uninstall").exists())
        with self.root_tgz() as tar:  # only there so ejecting restarts the Kobo
            self.assertEqual([m.name for m in tar], ["./usr/local/kobodash"])
        # what autostart.sh does at the next boot (see DeviceScripts), then the second run:
        (self.kobo / ".kobo/KoboRoot.tgz").unlink()
        (self.kobo / ".adds/kobodash/uninstall").unlink()
        (self.kobo / ".adds/kobodash/uninstalled").write_text("")
        self.run_cli(["yes"], "uninstall")
        self.assertFalse((self.kobo / ".adds/kobodash").exists())
        self.assertFalse((self.kobo / ".adds/nm/kobodash").exists())
        self.assertEqual((self.kobo / "My Book.epub").read_bytes(), b"book")

    def test_nothing_to_uninstall(self):
        self.run_cli([], "uninstall")
        self.assertIn("not installed", self.said())
        self.assertFalse((self.kobo / ".kobo" / "KoboRoot.tgz").exists())


class Helpers(unittest.TestCase):
    def test_read_info(self):
        with tempfile.TemporaryDirectory() as d:
            kobo = Path(d)
            (kobo / ".kobo").mkdir()
            for line, expected in ((CLARA_2E, ("4.38.23828", "386", "Kobo Clara 2E")),
                                   (CLARA_2E.replace("386", "999"), ("4.38.23828", "999", None)),
                                   ("garbage\n", None), ("", None)):
                (kobo / ".kobo" / "version").write_text(line)
                got = kd.read_info(kobo)
                self.assertEqual(got and (got.firmware, got.device_id, got.model and got.model.name), expected)

    def test_server_base(self):
        for text, expected in (("192.168.1.50", "http://192.168.1.50:8737"), ("192.168.1.50:9000", "http://192.168.1.50:9000"),
                               ("http://kobo.example.com:8737/api/", "http://kobo.example.com:8737"),
                               ("https://kobo.example.com", "https://kobo.example.com"), ("", ""), ("ftp://x", ""),
                               ("host:99999", "")):
            self.assertEqual(kd.server_base(text), expected, text)

    def test_parse_lsblk(self):
        sample = json.dumps({"blockdevices": [
            {"name": "nvme0n1", "label": None, "size": "512G", "fstype": None, "mountpoints": [None], "rm": False,
             "hotplug": False, "tran": "nvme", "children": [
                 {"name": "nvme0n1p2", "label": "root", "size": "500G", "fstype": "ext4", "mountpoints": ["/"],
                  "rm": False, "hotplug": False, "tran": None}]},
            {"name": "sda", "label": None, "size": "29.8G", "fstype": None, "mountpoints": [None], "rm": True,
             "hotplug": True, "tran": "usb", "children": [
                 {"name": "sda1", "label": "KOBOeReader", "size": "29.8G", "fstype": "vfat",
                  "mountpoints": ["/run/media/me/KOBOeReader"], "rm": True, "hotplug": True, "tran": None}]},
            {"name": "sdb", "label": None, "size": "8G", "fstype": None, "mountpoints": [None], "rm": "1",
             "hotplug": "1", "tran": "usb", "children": [
                 {"name": "sdb1", "label": None, "size": "8G", "fstype": "vfat", "mountpoints": [None], "rm": "1",
                  "hotplug": "1", "tran": None}]}]})
        drives, unmounted = kd.parse_lsblk(sample)
        self.assertEqual(drives, [kd.Drive("KOBOeReader", "29.8G", Path("/run/media/me/KOBOeReader"))])
        self.assertEqual(unmounted, ["/dev/sdb1"])

    def test_pins_are_well_formed(self):
        pins = json.loads((ROOT / "cli" / "pins.json").read_text())
        self.assertRegex(pins["commit"], r"^[0-9a-f]{40}$")
        hashes = [pins["nickelmenu"]["sha256"], pins["kobostuff"]["sha256"], *pins["client"].values()]
        self.assertTrue(all(len(h) == 64 and int(h, 16) >= 0 for h in hashes))
        self.assertIn("trmnl.sh", pins["client"])


class DeviceScripts(unittest.TestCase):
    def test_every_shell_script_parses(self):
        scripts = sorted((ROOT / "device").rglob("*.sh"))
        self.assertGreaterEqual(len(scripts), 4)
        for script in scripts:
            with self.subTest(script.relative_to(ROOT).as_posix()):
                self.assertEqual(subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True).stderr, "")

    def sandbox(self, files=(), marker=False, keys=False):
        """A copy of the client and a fake Kobo root for autostart.sh: returns (client dir, fake root)."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        client, fake = tmp / "client", tmp / "root"
        shutil.copytree(ROOT / "device" / "client", client)
        for name in files:
            (client / name).write_text("3" if name == "bootcount" else "")
        for folder in ("usr/local/kobodash", "etc/udev/rules.d", "mnt/onboard/.adds/nm", "usr/bin", "tmp", "usr/local/niluje/usbnet/etc"):
            (fake / folder).mkdir(parents=True)
        (fake / "etc/udev/rules.d/99-kobodash.rules").write_text("rule")
        if marker:
            (fake / "usr/local/kobodash/installed-nickelmenu").write_text("")
        if keys:
            (fake / "usr/local/niluje/usbnet/etc/authorized_keys").write_text(PUBKEY)
        stub = fake / "usr/bin/dropbear"
        stub.write_text('#!/bin/sh\necho "$@" > "$0.args"\n')
        stub.chmod(0o755)
        return client, fake

    @staticmethod
    def cable(fake, device, battery, status):
        """The cable as the kernel and the battery report it: usb_host (computer) or usb_plug (charger)."""
        (fake / "sys/devices/platform" / device).mkdir(parents=True)
        (fake / "sys/class/power_supply" / battery).mkdir(parents=True)
        (fake / "sys/class/power_supply" / battery / "status").write_text(status + "\n")

    def run_autostart(self, client, fake, timeout=30):
        return subprocess.run(["sh", str(client / "autostart.sh")], env={**os.environ, "KOBODASH_ROOT": str(fake)},
                              capture_output=True, text=True, timeout=timeout)

    def boot(self, files=(), marker=False, keys=False):
        """Run autostart.sh as the Kobo would at boot, in a sandbox: returns (client dir, fake root)."""
        client, fake = self.sandbox(files, marker, keys)
        done = self.run_autostart(client, fake)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        return client, fake

    def test_a_computer_is_a_usb_host_with_the_battery_on_power(self):
        for device, battery, status, expected in [
            ("usb_host", "battery", "Charging", 0),
            ("usb_host", "battery", "Full", 0),
            ("usb_host", "mc13892_bat", "Charging", 0),  # older Kobos
            ("usb_host", "battery", "Discharging", 1),   # the kernel keeps the name after unplugging
            ("usb_plug", "battery", "Charging", 1),      # a wall charger
        ]:
            with self.subTest(device=device, battery=battery, status=status):
                fake = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, fake, ignore_errors=True)
                self.cable(fake, device, battery, status)
                done = subprocess.run(["sh", str(ROOT / "device/client/scripts/computer.sh")],
                                      env={**os.environ, "KOBODASH_ROOT": str(fake)}, capture_output=True, text=True)
                self.assertEqual((done.returncode, done.stderr), (expected, ""))

    def test_after_a_computer_the_dashboard_starts_again_once_unplugged(self):
        # bootcount at 3 makes the second pass stop and say so, which shows the start path ran again
        client, _ = self.boot(["nickel-once", "bootcount"])
        self.assertFalse((client / "nickel-once").exists())
        log = (client / "debug.log").read_text()
        self.assertIn("plugged into a computer", log)
        self.assertIn("3 boots without asking the server", log)

    def test_nickel_keeps_the_screen_while_a_computer_is_plugged_in(self):
        client, fake = self.sandbox(["nickel-once"])
        self.cable(fake, "usb_host", "battery", "Charging")
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_autostart(client, fake, timeout=3)
        self.assertFalse((client / "bootcount").exists())  # the dashboard never started

    def test_uninstall_flag_removes_the_hook(self):
        client, fake = self.boot(["uninstall"], marker=True)
        self.assertFalse((fake / "etc/udev/rules.d/99-kobodash.rules").exists())
        self.assertFalse((fake / "usr/local/kobodash").exists())
        self.assertTrue((fake / "mnt/onboard/.adds/nm/uninstall").exists())  # NickelMenu goes too: we installed it
        self.assertFalse((client / "uninstall").exists())
        self.assertTrue((client / "uninstalled").exists())

    def test_uninstall_leaves_a_nickelmenu_we_did_not_install(self):
        _, fake = self.boot(["uninstall"], marker=False)
        self.assertFalse((fake / "mnt/onboard/.adds/nm/uninstall").exists())
        self.assertFalse((fake / "usr/local/kobodash").exists())

    def test_disabled_file_and_three_failed_boots_stay_in_nickel(self):
        client, _ = self.boot(["disabled"])
        self.assertFalse((client / "bootcount").exists())
        client, _ = self.boot(["bootcount"])
        self.assertEqual((client / "bootcount").read_text(), "3")
        self.assertIn("3 boots without asking the server", (client / "debug.log").read_text())

    def test_open_servers_stay_off_and_ssh_runs_only_with_a_key(self):
        for keys in (False, True):
            with self.subTest(keys=keys):
                client, fake = self.boot(["disabled"], keys=keys)  # `disabled` ends the script right after the SSH part
                flags = fake / "usr/local/niluje/usbnet/etc"
                self.assertTrue((flags / "NO_SSH").exists() and (flags / "NO_TELNET").exists())
                args = fake / "usr/bin/dropbear.args"
                self.assertEqual(args.exists(), keys)
                if keys:
                    self.assertIn("-s", args.read_text().split())  # no password logins


if __name__ == "__main__":
    unittest.main()
