"""Drive the shipped Antigravity fetch/parse helpers."""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SKIN_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = SKIN_ROOT.parent / "@Resources" / "Antigravity"
SHARED_RESOURCES = SKIN_ROOT.parent / "@Resources"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(RESOURCES))

import fetch  # noqa: E402


def _has_used_zero(snapshot: dict) -> bool:
    for key in ("session_used", "weekly_used"):
        if key in snapshot and snapshot[key] == 0:
            return True
    return False


class RemainingMathTests(unittest.TestCase):
    def test_used_is_one_hundred_minus_remaining_fraction(self) -> None:
        self.assertAlmostEqual(fetch.used_from_remaining_fraction(1), 0.0)
        self.assertAlmostEqual(fetch.used_from_remaining_fraction(0.5), 50.0)
        self.assertAlmostEqual(fetch.used_from_remaining_fraction(0), 100.0)

    def test_used_clamped_to_unit_interval(self) -> None:
        self.assertEqual(fetch.used_from_remaining_fraction(1.2), 0.0)
        self.assertEqual(fetch.used_from_remaining_fraction(-0.1), 100.0)


class CountdownTests(unittest.TestCase):
    def test_iso_and_seconds(self) -> None:
        now = datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(fetch.format_countdown("2099-01-01T12:00:00Z", now=now), "2h")
        self.assertEqual(fetch.format_countdown("2099-01-04T00:00:00+00:00", now=now), "2d 14h")
        self.assertEqual(fetch.format_countdown("", now=now), "--")
        self.assertEqual(fetch.format_countdown_seconds(0), "--")
        self.assertEqual(fetch.format_countdown_seconds(-9000), "--")


class ParseSummaryTests(unittest.TestCase):
    def test_valid_fixture_uses_gemini_remaining(self) -> None:
        payload = json.loads((FIXTURES / "valid_quota.json").read_text(encoding="utf-8"))
        now = datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
        snap = fetch.parse_quota_summary(payload, now=now)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["error"], "")
        self.assertAlmostEqual(snap["session_used"], 0.0, places=5)
        self.assertAlmostEqual(snap["session_remaining"], 100.0, places=5)
        self.assertAlmostEqual(snap["weekly_used"], 4.990005, places=5)
        self.assertAlmostEqual(snap["weekly_remaining"], 95.009995, places=5)
        self.assertEqual(snap["session_reset"], "2h")
        self.assertEqual(snap["weekly_reset"], "6d 14h")
        self.assertEqual(snap["session_resets_at"], "2099-01-01T12:00:00Z")
        self.assertEqual(snap["weekly_resets_at"], "2099-01-08T00:00:00Z")

    def test_nested_remaining_object(self) -> None:
        snap = fetch.parse_quota_summary(
            {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-5h",
                                "window": "5h",
                                "remaining": {"remainingFraction": 0.25},
                                "resetTime": "2099-01-01T12:00:00Z",
                            }
                        ],
                    }
                ]
            },
            now=datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["session_used"], 75.0)
        self.assertIsNone(snap["weekly_used"])

    def test_malformed_is_error_not_zero(self) -> None:
        snap = fetch.parse_quota_summary({"unexpected": True})
        self.assertFalse(snap["ok"])
        self.assertTrue(snap["error"])
        self.assertNotIn("session_used", snap)
        self.assertFalse(_has_used_zero(snap))


class HttpAndBuildTests(unittest.TestCase):
    def test_http_401_is_explicit(self) -> None:
        snap = fetch.snapshot_from_http(401, b"{}")
        self.assertFalse(snap["ok"])
        self.assertIn("expired", snap["error"].lower())
        self.assertNotIn("session_used", snap)

    def test_http_429_is_explicit(self) -> None:
        snap = fetch.snapshot_from_http(429, b"{}")
        self.assertFalse(snap["ok"])
        self.assertIn("rate limited", snap["error"].lower())

    def test_malformed_body(self) -> None:
        snap = fetch.snapshot_from_http(200, (FIXTURES / "malformed_body.txt").read_bytes())
        self.assertFalse(snap["ok"])
        self.assertIn("malformed", snap["error"].lower())

    def test_missing_server_is_explicit_error(self) -> None:
        snap = fetch.build_snapshot(servers=[])
        self.assertFalse(snap["ok"])
        self.assertIn("language server not found", snap["error"].lower())
        self.assertNotIn("session_used", snap)

    def test_build_snapshot_uses_injected_local_server(self) -> None:
        body = (FIXTURES / "valid_quota.json").read_bytes()
        captured: dict = {}

        def opener(url, headers, timeout, data=None):
            captured["url"] = url
            captured["headers"] = dict(headers)
            return 200, body

        snap = fetch.build_snapshot(
            opener=opener,
            now=datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc),
            servers=[
                {
                    "pid": 1,
                    "kind": "app",
                    "csrf": "test-csrf",
                    "ports": [64944],
                }
            ],
        )
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["source"], "local-app")
        self.assertIn("RetrieveUserQuotaSummary", captured["url"])
        self.assertEqual(captured["headers"]["X-Codeium-Csrf-Token"], "test-csrf")
        self.assertAlmostEqual(snap["weekly_remaining"], 95.009995, places=5)


class ClassifyTests(unittest.TestCase):
    def test_app_vs_ide(self) -> None:
        self.assertEqual(
            fetch._classify_kind(
                r"C:\x\antigravity\resources\bin\language_server.exe --standalone --app_data_dir antigravity"
            ),
            "app",
        )
        self.assertEqual(
            fetch._classify_kind(
                r"C:\x\antigravity\bin\language_server_windows_x64.exe --app_data_dir antigravity-ide --subclient_type ide"
            ),
            "ide",
        )
        self.assertEqual(fetch._extract_csrf("--csrf_token abc-def --extension_server_port 1"), "abc-def")


class CarryForwardTests(unittest.TestCase):
    """A failed refresh must never blank a good reading.

    The language server is only reachable while Antigravity is running, so
    failures are routine. Before this, any of them overwrote the snapshot with a
    bare error object and the skin lost every value it had.
    """

    GOOD = {
        "ok": True,
        "session_used": 12.0,
        "weekly_used": 5.9,
        "session_reset_unix": 1786955399,
        "error": "",
        "fetched_at": 1000,
        "checked_at": 1000,
        "last_error": "",
    }

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "snapshot.json"

    def test_failure_keeps_previous_values_and_fetched_at(self) -> None:
        self.target.write_text(json.dumps(self.GOOD), encoding="utf-8")
        merged = fetch.carry_forward(
            fetch.error_snapshot("Language server unreachable"), self.target, 2000
        )
        self.assertTrue(merged["ok"])
        self.assertEqual(merged["session_used"], 12.0)
        self.assertEqual(merged["weekly_used"], 5.9)
        self.assertEqual(merged["fetched_at"], 1000, "data did not get any newer")
        self.assertEqual(merged["checked_at"], 2000, "but we did just try")
        self.assertEqual(merged["last_error"], "Language server unreachable")

    def test_success_stamps_both_and_clears_error(self) -> None:
        self.target.write_text(
            json.dumps({**self.GOOD, "last_error": "boom"}), encoding="utf-8"
        )
        merged = fetch.carry_forward({"ok": True, "session_used": 13.0}, self.target, 3000)
        self.assertEqual(merged["session_used"], 13.0)
        self.assertEqual(merged["fetched_at"], 3000)
        self.assertEqual(merged["checked_at"], 3000)
        self.assertEqual(merged["last_error"], "")

    def test_failure_without_usable_prior_stays_not_ok(self) -> None:
        for label, prepare in (
            ("missing file", lambda: None),
            ("unparseable file", lambda: self.target.write_text("{oh no", encoding="utf-8")),
            (
                "prior also failed",
                lambda: self.target.write_text(
                    json.dumps({"ok": False, "error": "boom"}), encoding="utf-8"
                ),
            ),
        ):
            with self.subTest(prior=label):
                if self.target.exists():
                    self.target.unlink()
                prepare()
                merged = fetch.carry_forward(
                    fetch.error_snapshot("boom"), self.target, 4000
                )
                self.assertFalse(merged["ok"])
                self.assertEqual(merged["last_error"], "boom")
                self.assertFalse(_has_used_zero(merged))


class SkinWiringTests(unittest.TestCase):
    def test_ini_binds_required_fields_and_shipped_fetch(self) -> None:
        ini = (SKIN_ROOT / "Antigravity.ini").read_text(encoding="utf-8")
        self.assertIn("[Rainmeter]", ini)
        self.assertIn("fetch.cmd", ini)
        self.assertIn("Program=cmd", ini)
        self.assertIn('Parameter=/c ""#@#Antigravity\\fetch.cmd""', ini)
        self.assertIn("SessionUsed", ini)
        self.assertIn("WeeklyUsed", ini)
        self.assertIn("Session (5h)", ini)
        self.assertIn("Weekly (7d)", ini)
        self.assertLess(ini.find("Session (5h)"), ini.find("Weekly (7d)"))
        lua = (RESOURCES / "parse.lua").read_text(encoding="utf-8")
        self.assertIn('extract_number(raw, "session_used")', lua)
        self.assertIn('extract_number(raw, "weekly_used")', lua)
        self.assertNotIn("session_remaining", lua)
        self.assertIn("SessionReset", ini)
        self.assertIn("WeeklyReset", ini)
        self.assertIn("#Error#", ini)
        self.assertIn("MeasureFetch", ini)
        self.assertIn("FETCH_EVERY = 300", lua)
        self.assertIn("APPLY_EVERY = 5", lua)
        self.assertIn('CommandMeasure", "MeasureFetch", "Kill"', lua)
        self.assertIn("GetValue() == 0", lua)
        self.assertIn("now - lastCheckedAt", lua)
        self.assertIn("FETCH_MAX", lua)
        # The backoff must key off checked_at, not off every Apply() read.
        self.assertIn("checkedAt ~= lastCheckedAt", lua)
        self.assertIn('find("rate limit"', lua)
        self.assertIn("rateLimited", lua)
        self.assertIn('lastError == "" or rateLimited', lua)
        self.assertIn("TickCountdowns", lua)
        self.assertIn("Plugin=RunCommand", ini)
        self.assertIn("UpdateDivider=1", ini)
        self.assertNotIn("UpdateDivider=-1", ini)
        self.assertIn("Background=#@#Background.png", ini)
        self.assertIn("BackgroundMode=3", ini)
        self.assertIn("BackgroundMargins=0,34,0,14", ini)
        self.assertTrue((SHARED_RESOURCES / "Background.png").is_file())
        self.assertTrue((RESOURCES / "fetch.cmd").is_file())

    def test_runcommand_timeout_is_milliseconds_not_seconds(self) -> None:
        """RunCommand's Timeout is MILLISECONDS, and State=Hide makes it KILL.

        This shipped as Timeout=25, so Rainmeter killed cmd.exe 25ms after
        launch -- far too early for it to even spawn Python. fetch.py never ran,
        snapshot.json never changed, and the skin sat on day-old numbers while
        FinishAction kept firing as though everything were healthy.
        """
        ini = (SKIN_ROOT / "Antigravity.ini").read_text(encoding="utf-8")
        found = re.search(r"^Timeout=(\d+)", ini, re.MULTILINE)
        self.assertIsNotNone(found, "MeasureFetch must set an explicit Timeout")
        assert found is not None
        self.assertGreaterEqual(
            int(found.group(1)),
            15000,
            "Timeout is in milliseconds and must clear the fetcher's own HTTP timeout",
        )
        self.assertIn("ScriptFile=#@#Antigravity\\parse.lua", ini)


def local_identity() -> tuple[str, ...]:
    """Markers for whoever is running this, derived at runtime.

    Hardcoding a username or an email fragment would do the wrong thing twice:
    it would miss every other contributor's paths, and it would publish the
    author's own identity to anyone reading the repo. Short values are dropped
    so a two-letter username cannot match half the tree.
    """
    candidates = [str(Path.home()), Path.home().name, os.environ.get("USERNAME", "")]
    return tuple(m for m in dict.fromkeys(candidates) if len(m) >= 4)


class SecretHygieneTests(unittest.TestCase):
    FORBIDDEN = (
        "ya29.",
        "1//0",
        "sk-ant" + "-",
    )

    def test_tree_has_no_tokens_or_local_account_paths(self) -> None:
        hits: list[str] = []
        shipped = [SKIN_ROOT / "Antigravity.ini", *RESOURCES.iterdir()]
        for path in shipped:
            if not path.is_file():
                continue
            if path.suffix == ".pyc" or path.name == "snapshot.json":
                continue
            if path.suffix.lower() in {".png", ".bmp", ".jpg"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in self.FORBIDDEN + local_identity():
                if needle in text:
                    hits.append(f"{path}: {needle}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
