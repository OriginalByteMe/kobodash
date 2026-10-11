"""Every dashboards/<name>/ folder is well formed and its template renders offline (jinja2 only, no Chromium)."""
from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jinja2

ROOT = Path(__file__).resolve().parent.parent / "dashboards"
FOLDERS = sorted(p for p in ROOT.iterdir() if p.is_dir())
HTML = [p for p in FOLDERS if json.loads((p / "dashboard.json").read_text())["type"] == "html"]
NOW = datetime(2026, 10, 13, 8, 42, tzinfo=timezone(timedelta(hours=1)))


def render(folder: Path, src: dict, errors: dict | None = None, now=NOW, size=(1072, 1448)) -> str:
    """The template context the server builds (see the contract), with StrictUndefined like the server."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(folder), autoescape=True, undefined=jinja2.StrictUndefined)
    return env.get_template("dashboard.html").render(
        src=src, errors=errors or {}, now=now, next=now + timedelta(seconds=180),
        device={"id": "My Kobo", "battery": "74", "charging": False, "rssi": "-58", "fw": "1.0.0"},
        screen={"width": size[0], "height": size[1], "color": False}, font_dir="file:///fonts")


def sources(folder: Path) -> dict:
    return json.loads((folder / "dashboard.json").read_text()).get("sources", {})


def sample(folder: Path) -> dict:
    return json.loads((folder / "sample.json").read_text())


class TestDashboardFolders(unittest.TestCase):
    def test_shipped_dashboards(self):
        self.assertTrue({"home", "weather", "server", "cards", "agenda", "color", "url"} <= {p.name for p in FOLDERS})

    def test_config_and_sample(self):
        for d in FOLDERS:
            with self.subTest(d.name):
                cfg = json.loads((d / "dashboard.json").read_text())
                self.assertTrue(cfg.get("name") and cfg.get("description"), "name and description")
                self.assertIn(cfg.get("type"), ("html", "url"))
                if cfg["type"] == "url":
                    self.assertTrue(cfg.get("url"), "a url dashboard needs a url")
                    continue
                self.assertTrue(cfg["sources"], "an html dashboard needs sources")
                self.assertEqual(set(cfg["sources"]) - set(sample(d)), set(), "sample.json must cover every source")
                text = (d / "dashboard.html").read_text()
                self.assertIn("default-src 'none'", text)
                used = set(re.findall(r"src\.get\('(\w+)'\)", text))
                self.assertEqual(used - set(cfg["sources"]), set(), "template reads a source dashboard.json does not declare")


class TestRender(unittest.TestCase):
    def test_every_state(self):
        for d in HTML:
            names = list(sources(d))
            states = {
                "sample": (sample(d), {}),
                "sample with errors": (sample(d), {n: "boom" for n in names}),
                "every source failed": ({n: None for n in names}, {n: "boom" for n in names}),
                "no sources": ({}, {}),
            }
            for label, (src, errors) in states.items():
                with self.subTest(f"{d.name}: {label}"):
                    html = render(d, src, errors)
                    self.assertIn("</html>", html)
                    if errors:
                        self.assertIn("boom", html, "errors are shown")

    def test_other_screen_sizes(self):
        for d in HTML:
            for size in ((1264, 1680), (758, 1024)):
                with self.subTest(f"{d.name} at {size}"):
                    self.assertIn("</html>", render(d, sample(d), size=size))


class TestCards(unittest.TestCase):
    folder = ROOT / "cards"

    def cards(self, payload) -> str:
        return render(self.folder, {"cards": payload})

    def test_card_counts(self):
        card = {"title": "Door", "value": "Locked", "note": "since 07:12", "icon": "lock", "items": ["a", "b", "c", "d"]}
        for n, shown in [(0, 0), (1, 1), (2, 2), (3, 3), (5, 5), (8, 8), (9, 9), (12, 12), (15, 12)]:
            with self.subTest(n):
                html = self.cards({"title": "T", "cards": [card] * n})
                self.assertEqual(html.count('class="card"'), shown)

    def test_optional_fields_and_bad_payloads(self):
        html = self.cards({"cards": [{"title": "Bare"}, {"title": "Number", "value": 42}, {"value": 0}]})
        self.assertEqual(html.count('class="card"'), 3)
        self.assertIn("42", html)
        self.assertIn("No cards", self.cards({"title": "T"}))
        self.assertIn("No cards", self.cards(["not", "an", "object"]))


class TestAgenda(unittest.TestCase):
    folder = ROOT / "agenda"

    def test_past_and_next(self):
        src = sample(self.folder)
        # timed items: 08:30 10:00 12:30 15:00 17:45 19:30; "All day" is never past or next
        morning = render(self.folder, src, now=NOW)
        self.assertEqual(morning.count("item past"), 1)
        self.assertEqual(morning.count("item next"), 1)
        night = render(self.folder, src, now=NOW.replace(hour=23, minute=0))
        self.assertEqual(night.count("item past"), 6)
        self.assertEqual(night.count("item next"), 0)

    def test_missing_fields_and_empty(self):
        html = render(self.folder, {"weather": None, "agenda": {"items": [{"title": "No time"}, {"time": None}]}})
        self.assertIn("No time", html)
        self.assertIn("Nothing planned", render(self.folder, {"agenda": {"items": []}}))


if __name__ == "__main__":
    unittest.main()
