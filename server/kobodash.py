#!/usr/bin/env python3
"""kobodash: renders a dashboard for a Kobo e-reader, speaking the TRMNL BYOS protocol.

Serve (needs KOBODASH_TOKEN; see .env.example):
  GET /api/display    headers ID, Access-Token, Battery-Percent, Charging, RSSI, FW-Version.
                      Renders dashboards/$DASHBOARD to a PNG and answers {image_url, filename,
                      refresh_rate, update_firmware, firmware_url, firmware_hmac}.
  GET /image/<n>.png  the latest render; <n> is random per render.
  GET /client.tar.gz  the device client from DEVICE_DIR, for over-the-air updates.
Preview once and exit:
  kobodash.py --preview <dashboard> --out preview.png [--sample] [--screen 1072x1448] [--color]

A dashboard is a folder with dashboard.json (+ dashboard.html, sample.json). Each source is fetched on its own:
one that fails becomes None in the template's `src` and a message in `errors`; the render goes on.
"""
from __future__ import annotations

import argparse
import configparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
import zipfile

import jinja2

ROOT = Path(__file__).resolve().parent.parent
E = os.environ.get
TOKEN = E("KOBODASH_TOKEN", "")
PUBLIC_URL = E("PUBLIC_URL", "http://127.0.0.1:8737").rstrip("/")
TZ = ZoneInfo(E("TZ", "UTC"))
LAT, LON = E("LAT", ""), E("LON", "")
REFRESH = int(E("REFRESH", "180"))
# Signs client updates. Shared with the Kobo's config.json at install and never sent over the
# wire (unlike the Access-Token header), so a LAN attacker cannot forge an update.
OTA_KEY = E("OTA_KEY", "")
DASHBOARD = E("DASHBOARD", "home")
PROC = Path(E("HOST_PROC", "/proc"))
HWMON = Path(E("HWMON", "/sys/class/hwmon"))
DISKS_INI = Path(E("DISKS_INI", "/var/local/emhttp/disks.ini"))
DASHBOARDS_DIR = ROOT / E("DASHBOARDS_DIR", "dashboards")  # ROOT / absolute path is that path
DEVICE_DIR = ROOT / E("DEVICE_DIR", "device/client")
FONT_DIR = ROOT / E("FONT_DIR", ".cache/fonts")
SCREEN, COLOR = (1072, 1448), False  # main() sets these from SCREEN/COLOR or the flags
LOCK = threading.Lock()
STATE = {"name": None, "png": None}


def log(msg: str) -> None:
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def fetch(url: str, headers: dict | None = None, timeout: int = 10) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
        return r.read()


def safe(name, fn):
    try:
        return fn()
    except Exception as e:  # an optional reading missing blanks that reading, not the source
        log(f"reading={name} error={e!r}")
        return None


_cache: dict = {}


def cached(key, ttl, fn):
    """fn() at most every ttl seconds; when it fails the last good value is kept (raises if there is none)."""
    at, value = _cache.get(key, (0.0, None))
    if value is None or time.monotonic() - at >= ttl:
        try:
            value = fn()
            _cache[key] = (time.monotonic(), value)
        except Exception as e:
            log(f"source={key[0]} error={e!r}")
            if value is None:
                raise
    return value


# --- weather and air (Open-Meteo) -------------------------------------------

WMO = {0: ("Clear", "clear_day"), 1: ("Mainly clear", "partly_cloudy_day"), 2: ("Partly cloudy", "partly_cloudy_day"),
       3: ("Overcast", "cloud"), 45: ("Fog", "foggy"), 48: ("Fog", "foggy"), 51: ("Light drizzle", "rainy_light"),
       53: ("Drizzle", "rainy_light"), 55: ("Heavy drizzle", "rainy_light"), 56: ("Freezing drizzle", "rainy_light"),
       57: ("Freezing drizzle", "rainy_light"), 61: ("Light rain", "rainy_light"), 63: ("Rain", "rainy"),
       65: ("Heavy rain", "rainy_heavy"), 66: ("Freezing rain", "rainy"), 67: ("Freezing rain", "rainy_heavy"),
       71: ("Light snow", "weather_snowy"), 73: ("Snow", "weather_snowy"), 75: ("Heavy snow", "weather_snowy"),
       77: ("Snow grains", "weather_snowy"), 80: ("Light showers", "rainy_light"), 81: ("Showers", "rainy"),
       82: ("Violent showers", "rainy_heavy"), 85: ("Snow showers", "weather_snowy"), 86: ("Snow showers", "weather_snowy"),
       95: ("Thunderstorm", "thunderstorm"), 96: ("Thunderstorm, hail", "thunderstorm"), 99: ("Thunderstorm, hail", "thunderstorm")}
NIGHT = {"clear_day": "bedtime", "partly_cloudy_day": "partly_cloudy_night"}


def sky(code, day=1) -> dict:
    label, icon = WMO.get(code, ("Unknown", "help"))
    return {"label": label, "icon": icon if day else NIGHT.get(icon, icon)}


def band(value, bands):
    """Label of the first (upper bound, label) band that value fits under."""
    return next(label for top, label in bands if top is None or value <= top)


def aqi_label(aqi) -> str:  # US EPA AQI categories
    return band(aqi, [(50, "Good"), (100, "Moderate"), (150, "Unhealthy for sensitive groups"),
                      (200, "Unhealthy"), (300, "Very unhealthy"), (None, "Hazardous")])


def uv_label(uv) -> str:  # WHO UV index categories
    return band(round(uv), [(2, "Low"), (5, "Moderate"), (7, "High"), (10, "Very high"), (None, "Extreme")])


def open_meteo(host: str, spec: dict, **params) -> dict:
    lat, lon = str(spec.get("lat") or LAT), str(spec.get("lon") or LON)
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "timezone": TZ.key, **params})
    return json.loads(fetch(f"https://{host}/v1/{'forecast' if host.startswith('api.') else 'air-quality'}?{q}"))


def weather(spec: dict, now: datetime) -> dict:
    w = open_meteo("api.open-meteo.com", spec, forecast_days=4, forecast_hours=9,
                   current="temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
                   hourly="temperature_2m,precipitation_probability,weather_code,is_day",
                   daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                         "precipitation_sum,uv_index_max,sunrise,sunset")
    c, h, d = w["current"], w["hourly"], w["daily"]
    day = lambda i: {"hi": d["temperature_2m_max"][i], "lo": d["temperature_2m_min"][i],
                     "rain_p": d["precipitation_probability_max"][i], "rain_mm": d["precipitation_sum"][i],
                     "name": datetime.fromisoformat(d["time"][i]).strftime("%a"), **sky(d["weather_code"][i])}
    return {"now": {"temp": c["temperature_2m"], "feels": c["apparent_temperature"], "humidity": c["relative_humidity_2m"],
                    "wind": c["wind_speed_10m"], **sky(c["weather_code"], c["is_day"])},
            "today": {**day(0), "uv": d["uv_index_max"][0], "uv_label": uv_label(d["uv_index_max"][0]),
                      "sunrise": d["sunrise"][0][-5:], "sunset": d["sunset"][0][-5:]},
            "hours": [{"hour": t[11:13], "temp": h["temperature_2m"][i], "rain_p": h["precipitation_probability"][i],
                       **sky(h["weather_code"][i], h["is_day"][i])} for i, t in enumerate(h["time"])][1:],
            "days": [day(i) for i in range(1, len(d["time"]))]}


def air(spec: dict, now: datetime) -> dict:
    a = open_meteo("air-quality-api.open-meteo.com", spec, current="us_aqi,pm2_5,pm10")["current"]
    return {"aqi": a["us_aqi"], "label": aqi_label(a["us_aqi"]), "pm25": a["pm2_5"], "pm10": a["pm10"]}


# --- the host (/proc, hwmon) and Unraid --------------------------------------------

_cpu = None


def cpu_busy() -> float:
    """CPU use in % since the previous render (half a second on the first)."""
    global _cpu

    def sample():
        v = [int(x) for x in (PROC / "stat").read_text().split("\n", 1)[0].split()[1:]]
        return v[3] + v[4], sum(v)  # idle + iowait, total

    prev = _cpu or sample()
    if _cpu is None:
        time.sleep(0.5)
    _cpu = sample()
    total = _cpu[1] - prev[1]
    return 100 * (1 - (_cpu[0] - prev[0]) / total) if total else 0.0


def cpu_temp():
    for label in HWMON.glob("hwmon*/temp*_label"):
        if label.read_text().strip() in ("Package id 0", "Tctl"):
            return int(label.with_name(label.name.replace("label", "input")).read_text()) / 1000
    return None


def ago(delta: timedelta) -> str:
    s = int(delta.total_seconds())
    return f"{s // 86400}d {s % 86400 // 3600}h" if s >= 86400 else f"{s // 3600}h {s % 3600 // 60}m"


def host(spec: dict, now: datetime) -> dict:
    mem = {k: int(v.split()[0]) for k, v in (l.split(":", 1) for l in (PROC / "meminfo").read_text().splitlines())}
    total, avail = mem["MemTotal"], mem["MemAvailable"]
    return {"cpu": cpu_busy(), "cpu_temp": safe("hwmon", cpu_temp), "cores": os.cpu_count(),
            "load": (PROC / "loadavg").read_text().split()[:3],
            "uptime": ago(timedelta(seconds=float((PROC / "uptime").read_text().split()[0]))),
            "mem_used": (total - avail) / 1024**2, "mem_total": total / 1024**2, "mem_pct": 100 * (total - avail) / total}


def ini(text: str) -> dict:
    """Unraid's emhttp ini files: ["section"] headers and key="value" lines."""
    p = configparser.RawConfigParser()
    p.optionxform = str  # keep Unraid's camelCase keys (fsSize, numErrors)
    p.read_string(text)
    return {s.strip('"'): {k: v.strip('"') for k, v in p[s].items()} for s in p.sections()}


def disks(text: str) -> list[dict]:
    """Present array, parity and pool devices; empty slots and the flash drive are skipped."""
    out = []
    for name, d in ini(text).items():
        if d.get("status", "").startswith("DISK_NP") or d.get("type") == "Flash":
            continue
        size, free = int(d.get("fsSize") or 0), int(d.get("fsFree") or 0)  # KiB
        out.append({"name": name, "type": d.get("type", ""), "status": d.get("status", "").removeprefix("DISK_"),
                    "temp": int(d["temp"]) if d.get("temp", "*").isdigit() else None,  # * = spun down
                    "errors": int(d.get("numErrors") or 0),
                    "used": (size - free) / size if size else None, "free_tb": free / 1024**3})
    return out


def parity(md: dict, now: datetime) -> str:
    """One line on the parity state from Unraid's /proc/mdstat fields."""
    pos, size = int(md.get("mdResyncPos") or 0), int(md.get("mdResyncSize") or 0)
    if pos and size:
        what = {"check": "Parity check", "recon": "Parity sync", "clear": "Clearing"}.get(
            md.get("mdResyncAction", "").split(" ")[0], "Parity operation")
        return f"{what} {pos * 100 // size}% · {'running' if int(md.get('mdResync') or 0) else 'paused'}"
    done = int(md.get("sbSynced2") or md.get("sbSynced") or 0)
    if not done:
        return "No parity check yet"
    errors = int(md.get("sbSyncErrs") or 0)
    return f"Last parity check {ago(now - datetime.fromtimestamp(done, TZ))} ago · {errors} error{'s' * (errors != 1)}"


def unraid(spec: dict, now: datetime) -> dict:
    md = dict(l.split("=", 1) for l in (PROC / "mdstat").read_text().splitlines() if "=" in l)
    return {"array": md.get("mdState", "?").capitalize(), "parity": parity(md, now),
            "disks": safe("disks", lambda: disks(DISKS_INI.read_text()))}


# --- Docker and ntfy -----------------------------------------------------------

def docker(spec: dict, now: datetime) -> dict:
    rows = json.loads(fetch(spec["url"].rstrip("/") + "/containers/json?all=1"))
    name = lambda r: r["Names"][0].lstrip("/")
    return {"running": sum(r["State"] == "running" for r in rows), "total": len(rows),
            "unhealthy": sorted(name(r) for r in rows if "(unhealthy)" in r["Status"]),
            "stopped": sorted(name(r) for r in rows if r["State"] != "running")}


def ntfy(spec: dict, now: datetime) -> list[dict]:
    topics = spec["topics"] if isinstance(spec["topics"], str) else ",".join(spec["topics"])
    token = {"Authorization": "Bearer " + spec["token"]} if spec.get("token") else {}
    raw = fetch(f"{spec['url'].rstrip('/')}/{topics}/json?poll=1&since=24h", token)
    msgs = [m for m in map(json.loads, raw.decode().splitlines()) if m.get("event") == "message"]
    out = []
    for m in sorted(msgs, key=lambda m: m["time"], reverse=True)[:5]:
        at = datetime.fromtimestamp(m["time"], TZ)
        out.append({"when": f"{at:%H:%M}" if at.date() == now.date() else f"{at:%a %H:%M}", "topic": m["topic"],
                    "title": m.get("title", ""), "message": m.get("message", ""), "urgent": m.get("priority", 3) >= 4})
    return out


BUILTINS = {"weather": (weather, 600), "air": (air, 900), "host": (host, 0), "unraid": (unraid, 0),
            "docker": (docker, 0), "ntfy": (ntfy, 0)}  # name: (function, default cache seconds)


# --- dashboards ----------------------------------------------------------------

def expand(value):
    """${VAR} in every string of a parsed dashboard.json, from the environment (unset = empty)."""
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: E(m[1], ""), value)
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value


def load_dashboard(name: str) -> dict:
    path = DASHBOARDS_DIR / name / "dashboard.json"
    if not path.is_file():
        raise RuntimeError(f"no dashboard {name!r}: {path} does not exist")
    return expand(json.loads(path.read_text(encoding="utf-8")))


class NotConfigured(Exception):
    """A source whose required setting is empty: skipped without fetching or reporting an error."""


def get_source(name: str, spec: dict, now: datetime):
    if "builtin" in spec:
        if spec["builtin"] not in BUILTINS:
            raise ValueError(f"unknown builtin {spec['builtin']!r}")
        fn, ttl = BUILTINS[spec["builtin"]]
        call = lambda: fn(spec, now)
        if spec["builtin"] in ("weather", "air"):
            need = (spec.get("lat") or LAT) and (spec.get("lon") or LON)
        else:
            need = spec["builtin"] not in ("docker", "ntfy") or spec.get("url")
    elif "url" in spec:
        ttl, need = 0, spec["url"]
        call = lambda: json.loads(fetch(spec["url"], spec.get("headers")))
    else:
        raise ValueError("source needs a url or a builtin")
    if not need:
        raise NotConfigured(name)
    key = (name, json.dumps({k: v for k, v in spec.items() if k != "cache"}, sort_keys=True))
    return cached(key, spec.get("cache", ttl), call)


def context(dash: str, cfg: dict, device: dict, sample: bool) -> dict:
    """What the template sees: src (name -> data or None), errors (name -> message), times, device, screen."""
    now = datetime.now(TZ)
    sources = cfg.get("sources", {})
    samples = json.loads((DASHBOARDS_DIR / dash / "sample.json").read_text(encoding="utf-8")) if sample else {}

    def one(item):
        name, spec = item
        try:
            if sample:
                return name, samples[name], None
            return name, get_source(name, spec, now), None
        except NotConfigured:  # a ${VAR} that is not set: the dashboard shows its empty state, not an error
            return name, None, None
        except Exception as e:  # one broken source blanks one panel, not the dashboard
            return name, None, f"{type(e).__name__}: {e}"[:100]

    with ThreadPoolExecutor(len(sources) or 1) as pool:
        results = list(pool.map(one, sources.items()))
    return {"src": {n: v for n, v, _ in results}, "errors": {n: e for n, _, e in results if e},
            "now": now, "next": now + timedelta(seconds=cfg.get("refresh", REFRESH)), "device": device,
            "screen": {"width": SCREEN[0], "height": SCREEN[1], "color": COLOR}, "font_dir": FONT_DIR.as_uri()}


# --- fonts ---------------------------------------------------------------------

# Inter 4.1 and Material Symbols Outlined (google/material-design-icons @ 49d4db35), checksum-pinned.
FONTS = [("MaterialSymbolsOutlined.ttf", None,
          "https://raw.githubusercontent.com/google/material-design-icons/49d4db35df873165d6bd6ba09b063c7dafbac2f4/"
          "variablefont/MaterialSymbolsOutlined%5BFILL,GRAD,opsz,wght%5D.ttf",
          "77ff1c20896afc6f1abf7d57fc408234dfd021ab050b63e0442e71d6c9f0d6b8"),
         ("InterVariable.ttf", "InterVariable.ttf", "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip",
          "9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e")]  # (file, member of the zip, url, sha256)


def ensure_fonts() -> None:
    """Download the fonts into FONT_DIR on first use."""
    for dest, member, url, sha in FONTS:
        if (FONT_DIR / dest).is_file():
            continue
        log(f"downloading {dest}")
        data = fetch(url, timeout=60)
        if hashlib.sha256(data).hexdigest() != sha:
            raise RuntimeError(f"{url} does not match its pinned checksum")
        if member:
            data = zipfile.ZipFile(io.BytesIO(data)).read(member)
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        (FONT_DIR / (dest + ".part")).write_bytes(data)
        (FONT_DIR / (dest + ".part")).replace(FONT_DIR / dest)


# --- rendering -----------------------------------------------------------------

def find_chromium() -> str:
    names = [E("CHROMIUM")] if E("CHROMIUM") else ["chromium", "chromium-browser", "google-chrome-stable", "google-chrome"]
    exe = next(filter(None, map(shutil.which, names)), None)
    if not exe:
        raise RuntimeError("Chromium not found: install chromium or set CHROMIUM=/path/to/chromium")
    return exe


def screenshot(url: str, wait: float = 0) -> bytes:
    """PNG of the page at url, SCREEN pixels, once its scripts have had `wait` seconds."""
    with tempfile.TemporaryDirectory() as d:
        shot = Path(d, "shot.png")
        run = subprocess.run([find_chromium(), "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                              "--hide-scrollbars", "--force-device-scale-factor=1", f"--window-size={SCREEN[0]},{SCREEN[1]}",
                              f"--user-data-dir={d}/profile", f"--screenshot={shot}",
                              *([f"--virtual-time-budget={int(wait * 1000)}"] if wait else []), url],
                             capture_output=True, timeout=60 + wait)
        if run.returncode:
            raise RuntimeError(f"chromium failed: {run.stderr.decode(errors='replace').strip()[-200:]}")
        return shot.read_bytes()


def render(device: dict, sample: bool = False) -> tuple[bytes, int]:
    """(PNG of DASHBOARD, refresh seconds)."""
    cfg = load_dashboard(DASHBOARD)
    if cfg.get("type", "html") == "url":
        png = screenshot(cfg["url"], cfg.get("wait", 0))
    else:
        ensure_fonts()
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(DASHBOARDS_DIR / DASHBOARD), autoescape=True,
                                 undefined=jinja2.StrictUndefined)
        html = env.get_template("dashboard.html").render(**context(DASHBOARD, cfg, device, sample))
        with tempfile.TemporaryDirectory() as d:
            page = Path(d, "page.html")
            page.write_text(html, encoding="utf-8")
            png = screenshot(page.as_uri())
    return png, cfg.get("refresh", REFRESH)


# --- over-the-air update and HTTP ------------------------------------------------

def package():
    """(version, tar.gz bytes, HMAC-SHA256 under OTA_KEY) of the device client.
    All None when there is no client or no key: unsigned updates are never offered."""
    if not OTA_KEY or not (DEVICE_DIR / "version.txt").is_file():
        return None, None, None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(DEVICE_DIR.rglob("*")):
            if p.is_file():
                tar.add(p, p.relative_to(DEVICE_DIR).as_posix())
    data = buf.getvalue()
    return (DEVICE_DIR / "version.txt").read_text().strip(), data, hmac.new(OTA_KEY.encode(), data, "sha256").hexdigest()


CLIENT_VERSION, CLIENT_TAR, CLIENT_HMAC = package()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/display":
            return self.display()
        if STATE["png"] and path == f"/image/{STATE['name']}.png":
            return self.send(200, STATE["png"], "image/png")
        if CLIENT_TAR and path == "/client.tar.gz":
            return self.send(200, CLIENT_TAR, "application/gzip")
        self.send(404, b"not found\n", "text/plain")

    def display(self):
        h = self.headers
        if not TOKEN or not hmac.compare_digest(h.get("Access-Token", "").encode(), TOKEN.encode()):
            log(f"display refused id={h.get('ID', '?')!r} from={self.client_address[0]}")
            return self.send(401, b'{"error":"unknown device"}', "application/json")
        device = {"id": h.get("ID", "?"), "battery": h.get("Battery-Percent"), "charging": h.get("Charging") == "1",
                  "rssi": h.get("RSSI"), "fw": h.get("FW-Version", "")}
        t0 = time.monotonic()
        try:
            with LOCK:
                png, refresh = render(device)
                STATE.update(name=secrets.token_urlsafe(16), png=png)
                name = STATE["name"]
        except Exception as e:
            log(f"render error={e!r}")
            return self.send(500, b'{"error":"render failed"}', "application/json")
        update = bool(CLIENT_VERSION) and device["fw"] != CLIENT_VERSION
        log(f"display id={device['id']} fw={device['fw']} battery={device['battery']} rssi={device['rssi']} "
            f"render_ms={int(1000 * (time.monotonic() - t0))} bytes={len(png)} update={update}")
        self.send(200, json.dumps({"image_url": f"{PUBLIC_URL}/image/{name}.png", "filename": name,
                                   "refresh_rate": refresh, "update_firmware": update,
                                   "firmware_url": f"{PUBLIC_URL}/client.tar.gz", "firmware_hmac": CLIENT_HMAC}).encode(),
                  "application/json")


def main() -> None:
    global DASHBOARD, SCREEN, COLOR
    ap = argparse.ArgumentParser(description="Render a dashboard for a Kobo; serves it unless --preview is given.")
    ap.add_argument("--preview", metavar="DASHBOARD", help="render dashboards/DASHBOARD once and exit")
    ap.add_argument("--out", default="preview.png", help="where --preview writes the PNG (default preview.png)")
    ap.add_argument("--sample", action="store_true", help="with --preview: use sample.json, no network")
    ap.add_argument("--screen", default=E("SCREEN", "1072x1448"), metavar="WxH")
    ap.add_argument("--color", action="store_true", default=E("COLOR") == "1", help="a colour screen (templates see screen.color)")
    args = ap.parse_args()
    if args.sample and not args.preview:
        ap.error("--sample needs --preview")
    size = re.fullmatch(r"(\d+)x(\d+)", args.screen.lower())
    if not size:
        raise SystemExit(f"bad screen size {args.screen!r}: expected WxH, like 1072x1448")
    SCREEN, COLOR, DASHBOARD = (int(size[1]), int(size[2])), args.color, args.preview or DASHBOARD
    if not (args.preview or TOKEN):
        raise SystemExit("KOBODASH_TOKEN is not set: copy .env.example to .env and set it")
    try:
        find_chromium()
        load_dashboard(DASHBOARD)
        if args.preview:
            png, _ = render({"id": "preview", "battery": "87", "charging": False, "rssi": "-55", "fw": "preview"}, args.sample)
            Path(args.out).write_bytes(png)
            return print(f"{args.out}: {SCREEN[0]}x{SCREEN[1]} {'color' if COLOR else 'grayscale'}, {len(png)} bytes")
    except (RuntimeError, OSError) as e:
        raise SystemExit(f"error: {e}")
    log(f"kobodash on :{E('PORT', '8737')} dashboard={DASHBOARD} client={CLIENT_VERSION}")
    ThreadingHTTPServer(("", int(E("PORT", "8737"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
