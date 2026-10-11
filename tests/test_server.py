"""server/kobodash.py: the parsers, the dashboard model (${VAR}, sources that fail alone, cached JSON sources),
and the HTTP contract the Kobo relies on: only the device token gets a render, each
render gets a fresh image name, and the over-the-air update is signed with the OTA key over exactly the tarball
served. No network and no Chromium: fetch and screenshot are stubbed.
"""
from datetime import datetime
import hmac
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

SERVER = Path(__file__).resolve().parents[1] / "server" / "kobodash.py"
TMP = tempfile.TemporaryDirectory()
DEVICE, DASHBOARDS = Path(TMP.name, "client"), Path(TMP.name, "dashboards")
(DEVICE / "scripts").mkdir(parents=True)
(DEVICE / "version.txt").write_text("2.0.0\n")
(DEVICE / "scripts" / "loop.sh").write_text("echo hi\n")
for name, files in {
    "t": {"dashboard.json": {"type": "html", "refresh": 90, "sources": {
              "ok": {"url": "http://x/ok", "headers": {"Authorization": "Bearer ${TEST_TOKEN}"}},
              "bad": {"url": "http://x/bad"}}},
          "dashboard.html": "{{ src.ok.n }}|{{ src.bad }}|{{ errors.get('bad', '') }}|{{ errors|length }}|{{ screen.width }}|{{ device.id }}",
          "sample.json": {"ok": {"n": 3}, "bad": "four"}},
    "u": {"dashboard.json": {"type": "url", "url": "http://x/${TEST_HOST}", "wait": 2}},
}.items():
    Path(DASHBOARDS, name).mkdir(parents=True)
    for file, body in files.items():
        Path(DASHBOARDS, name, file).write_text(body if isinstance(body, str) else json.dumps(body))
os.environ.update(KOBODASH_TOKEN="device-token", OTA_KEY="ota-key", TZ="Asia/Kuala_Lumpur", DEVICE_DIR=str(DEVICE),
                  DASHBOARDS_DIR=str(DASHBOARDS), LAT="", LON="", TEST_TOKEN="s3cret", TEST_HOST="home")
spec = importlib.util.spec_from_file_location("kobodash", SERVER)
k = importlib.util.module_from_spec(spec)
sys.modules["kobodash"] = k
spec.loader.exec_module(k)

DISKS = '''["parity"]
type="Parity"
status="DISK_OK"
temp="43"
numErrors="0"
fsSize=""
["disk1"]
type="Data"
status="DISK_OK"
temp="45"
numErrors="2"
fsSize="5858435620"
fsFree="1784231924"
["disk2"]
type="Data"
status="DISK_NP"
temp="*"
["disk3"]
type="Data"
status="DISK_INVALID"
temp="*"
numErrors="0"
fsSize="100"
fsFree="100"
["parity2"]
type="Parity"
status="DISK_NP_DSBL"
["flash"]
type="Flash"
status="DISK_OK"
fsSize="30015536"
fsFree="27086736"
'''


class Parsers(unittest.TestCase):
    def test_disks(self):
        rows = {d["name"]: d for d in k.disks(DISKS)}
        self.assertEqual(list(rows), ["parity", "disk1", "disk3"])  # empty slots and flash skipped
        self.assertEqual((rows["parity"]["temp"], rows["parity"]["used"]), (43, None))
        self.assertAlmostEqual(rows["disk1"]["used"], 1 - 1784231924 / 5858435620)
        self.assertEqual(rows["disk1"]["errors"], 2)
        self.assertEqual((rows["disk3"]["temp"], rows["disk3"]["status"], rows["disk3"]["used"]), (None, "INVALID", 0))

    def test_parity(self):
        now = datetime(2026, 10, 11, 16, 0, tzinfo=k.TZ)
        md = {"mdResyncAction": "check P", "mdResyncSize": "1000", "mdResyncPos": "940", "mdResync": "0"}
        self.assertEqual(k.parity(md, now), "Parity check 94% · paused")
        self.assertEqual(k.parity({**md, "mdResync": "1000"}, now), "Parity check 94% · running")
        self.assertEqual(k.parity({**md, "mdResyncAction": "recon P"}, now), "Parity sync 94% · paused")
        done = {"mdResyncPos": "0", "mdResyncSize": "1000", "sbSynced2": str(int(now.timestamp()) - 26 * 3600), "sbSyncErrs": "1"}
        self.assertEqual(k.parity(done, now), "Last parity check 1d 2h ago · 1 error")
        self.assertEqual(k.parity({}, now), "No parity check yet")

    def test_bands(self):
        for aqi, label in [(0, "Good"), (50, "Good"), (51, "Moderate"), (100, "Moderate"), (101, "Unhealthy for sensitive groups"),
                           (150, "Unhealthy for sensitive groups"), (151, "Unhealthy"), (200, "Unhealthy"),
                           (201, "Very unhealthy"), (300, "Very unhealthy"), (301, "Hazardous")]:
            self.assertEqual(k.aqi_label(aqi), label, aqi)
        for uv, label in [(0, "Low"), (2.4, "Low"), (2.6, "Moderate"), (5, "Moderate"), (6, "High"), (7, "High"),
                          (8, "Very high"), (10.4, "Very high"), (10.6, "Extreme")]:
            self.assertEqual(k.uv_label(uv), label, uv)

    def test_cpu_busy(self):
        with tempfile.TemporaryDirectory() as d:
            stat = Path(d, "stat")
            k.PROC, k._cpu = Path(d), None
            self.addCleanup(setattr, k, "PROC", Path("/proc"))
            stat.write_text("cpu  100 0 100 700 100 0 0 0 0 0\n")
            self.assertEqual(k.cpu_busy(), 0.0)  # no ticks between samples is idle, not 100%
            stat.write_text("cpu  200 0 100 1400 100 0 0 0 0 0\n")  # +800 ticks, 700 of them idle
            self.assertAlmostEqual(k.cpu_busy(), 12.5)


class Dashboards(unittest.TestCase):
    def setUp(self):
        self.fetched, self.shots = [], []

        def fetch(url, headers=None, timeout=10):
            self.fetched.append((url, headers))
            if url.endswith("/bad"):
                raise OSError("down")
            return json.dumps({"n": 7}).encode()

        def screenshot(url, wait=0):
            self.shots.append((url, wait))
            page = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)) if url.startswith("file:") else None
            self.html = page.read_text() if page else None
            return b"PNG"

        for name, stub in [("fetch", fetch), ("screenshot", screenshot), ("ensure_fonts", lambda: None)]:
            self.addCleanup(setattr, k, name, getattr(k, name))
            setattr(k, name, stub)
        k._cache.clear()
        self.addCleanup(setattr, k, "DASHBOARD", k.DASHBOARD)
        self.device = {"id": "my-kobo"}

    def test_env_vars_expand_everywhere_and_unset_ones_are_empty(self):
        self.assertEqual(k.expand({"a": ["x-${TEST_TOKEN}", {"b": "${TEST_NOPE}!"}], "n": 5}),
                         {"a": ["x-s3cret", {"b": "!"}], "n": 5})

    def test_one_failing_source_leaves_the_others_rendered(self):
        k.DASHBOARD = "t"
        png, refresh = k.render(self.device)
        self.assertEqual(self.html, "7|None|OSError: down|1|1072|my-kobo")
        self.assertEqual(refresh, 90)  # dashboard.json beats REFRESH
        self.assertIn(("http://x/ok", {"Authorization": "Bearer s3cret"}), self.fetched)
        self.assertEqual(png, b"PNG")  # the screenshot goes to the Kobo as it is

    def test_sample_renders_without_the_network(self):
        k.DASHBOARD = "t"
        k.render(self.device, sample=True)
        self.assertEqual((self.html, self.fetched), ("3|four||0|1072|my-kobo", []))

    def test_url_dashboard_screenshots_the_page(self):
        k.DASHBOARD = "u"
        k.render(self.device)
        self.assertEqual(self.shots, [("http://x/home", 2)])

    def test_unset_settings_mean_not_configured_not_an_error(self):
        cfg = k.expand({"sources": {"u": {"url": "${TEST_NOPE}"}, "d": {"builtin": "docker", "url": "${TEST_NOPE}"},
                                    "n": {"builtin": "ntfy", "url": "${TEST_NOPE}", "topics": "a"},
                                    "w": {"builtin": "weather"}, "a": {"builtin": "air"}}})  # LAT, LON are empty
        ctx = k.context("t", cfg, self.device, sample=False)
        self.assertEqual(ctx["src"], dict.fromkeys("udnwa"))
        self.assertEqual((ctx["errors"], self.fetched), ({}, []))

    def test_url_source_keeps_its_last_good_value(self):
        spec = {"url": "http://x/c", "cache": 300}
        self.assertEqual(k.get_source("c", spec, None), {"n": 7})
        self.assertEqual(k.get_source("c", spec, None), {"n": 7})
        self.assertEqual(len(self.fetched), 1)  # inside the TTL: not fetched again
        spec = {"url": "http://x/c", "cache": 0}
        self.assertEqual(k.get_source("c", spec, None), {"n": 7})
        self.assertEqual(len(self.fetched), 2)  # expired: fetched again
        k.fetch = lambda *a, **kw: (_ for _ in ()).throw(OSError("down"))
        self.assertEqual(k.get_source("c", spec, None), {"n": 7})  # failing: last good kept
        with self.assertRaises(OSError):
            k.get_source("never-worked", {"url": "http://x/new"}, None)


class Display(unittest.TestCase):
    def setUp(self):
        self.renders = 0

        def render(device, sample=False):
            self.renders += 1
            return f"PNG-{self.renders}".encode(), 120

        self.addCleanup(setattr, k, "render", k.render)
        k.render = render
        k.STATE.update(name=None, png=None)
        k.CLIENT_VERSION, k.CLIENT_TAR, k.CLIENT_HMAC = k.package()
        self.srv = k.ThreadingHTTPServer(("127.0.0.1", 0), k.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.addCleanup(setattr, k, "PUBLIC_URL", k.PUBLIC_URL)
        k.PUBLIC_URL = self.base

    def get(self, path, **headers):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers)) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def display(self, fw="1.0.8"):
        code, body = self.get("/api/display", **{"ID": "my-kobo", "Access-Token": "device-token", "FW-Version": fw})
        self.assertEqual(code, 200)
        return json.loads(body)

    def test_only_the_device_token_gets_a_render(self):
        self.assertEqual(self.get("/image/None.png")[0], 404)
        self.assertEqual(self.get("/api/display")[0], 401)
        self.assertEqual(self.get("/api/display", **{"Access-Token": "device-tokeN"})[0], 401)
        self.assertEqual(self.get("/api/display", **{"Access-Token": "dévice"})[0], 401)
        self.assertEqual(self.renders, 0)

    def test_each_render_gets_a_fresh_image_name(self):
        first = self.display()
        self.assertEqual(self.get(first["image_url"][len(self.base):]), (200, b"PNG-1"))
        second = self.display()
        self.assertNotEqual(first["image_url"], second["image_url"])
        self.assertEqual(self.get(first["image_url"][len(self.base):])[0], 404)
        self.assertEqual(self.get(second["image_url"][len(self.base):]), (200, b"PNG-2"))
        self.assertEqual(first["refresh_rate"], 120)

    def test_update_is_signed_over_the_served_client(self):
        old = self.display("1.0.8")
        self.assertTrue(old["update_firmware"])
        code, tar = self.get(old["firmware_url"][len(self.base):])
        self.assertEqual((code, hmac.new(b"ota-key", tar, "sha256").hexdigest()), (200, old["firmware_hmac"]))
        self.assertFalse(self.display("2.0.0")["update_firmware"])

    def test_no_key_or_no_version_means_no_update_offered(self):
        key, k.OTA_KEY = k.OTA_KEY, ""
        self.addCleanup(setattr, k, "OTA_KEY", key)
        self.assertEqual(k.package(), (None, None, None))
        k.OTA_KEY = key
        device, k.DEVICE_DIR = k.DEVICE_DIR, Path(TMP.name, "missing")
        self.addCleanup(setattr, k, "DEVICE_DIR", device)
        self.assertEqual(k.package(), (None, None, None))


if __name__ == "__main__":
    unittest.main()
