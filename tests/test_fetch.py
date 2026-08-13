"""Drive the shipped ClaudeUsage fetch/parse/format helpers."""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SKIN_ROOT = Path(__file__).resolve().parents[1] / "Skins" / "ClaudeUsage"
RESOURCES = SKIN_ROOT / "@Resources"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(RESOURCES))

import fetch  # noqa: E402  — shipped module under @Resources


def _has_remaining_zero(snapshot: dict) -> bool:
    for key in ("session_remaining", "weekly_remaining"):
        if key in snapshot and snapshot[key] == 0:
            return True
    return False


class RemainingMathTests(unittest.TestCase):
    def test_remaining_is_one_hundred_minus_utilization(self) -> None:
        self.assertEqual(fetch.remaining_from_utilization(27.5), 72.5)
        self.assertEqual(fetch.remaining_from_utilization(0), 100.0)
        self.assertEqual(fetch.remaining_from_utilization(100), 0.0)

    def test_remaining_clamped_to_unit_interval(self) -> None:
        self.assertEqual(fetch.remaining_from_utilization(-10), 100.0)
        self.assertEqual(fetch.remaining_from_utilization(150), 0.0)

    def test_used_plus_remaining_is_one_hundred(self) -> None:
        for used in (0, 0.4, 27.5, 61.0, 99.9, 100, -5, 140):
            remaining = fetch.remaining_from_utilization(used)
            clamped = fetch.clamp_pct(used)
            self.assertAlmostEqual(clamped + remaining, 100.0, places=5)


class CountdownTests(unittest.TestCase):
    def test_iso_resets_at_becomes_human_countdown(self) -> None:
        now = datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(
            fetch.format_countdown("2099-01-01T12:00:00+00:00", now=now),
            "2h",
        )
        self.assertEqual(
            fetch.format_countdown("2099-01-04T00:00:00+00:00", now=now),
            "2d 14h",
        )
        self.assertEqual(
            fetch.format_countdown("2099-01-01T10:45:00+00:00", now=now),
            "45m",
        )

    def test_zulu_and_past_and_missing(self) -> None:
        now = datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(fetch.format_countdown("2099-01-01T12:00:00Z", now=now), "2h")
        self.assertEqual(fetch.format_countdown("2099-01-01T09:00:00+00:00", now=now), "now")
        self.assertEqual(fetch.format_countdown("", now=now), "--")
        self.assertEqual(fetch.format_countdown(None, now=now), "--")


class ParseValidPayloadTests(unittest.TestCase):
    def test_valid_five_hour_and_seven_day_fixture(self) -> None:
        payload = json.loads((FIXTURES / "valid_usage.json").read_text(encoding="utf-8"))
        now = datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)
        snapshot = fetch.parse_usage(payload, now=now)

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["error"], "")
        self.assertEqual(
            snapshot["session_remaining"],
            fetch.remaining_from_utilization(payload["five_hour"]["utilization"]),
        )
        self.assertEqual(
            snapshot["weekly_remaining"],
            fetch.remaining_from_utilization(payload["seven_day"]["utilization"]),
        )
        self.assertAlmostEqual(
            snapshot["session_remaining"] + snapshot["session_used"],
            100.0,
            places=5,
        )
        self.assertAlmostEqual(
            snapshot["weekly_remaining"] + snapshot["weekly_used"],
            100.0,
            places=5,
        )
        self.assertEqual(snapshot["session_reset"], "2h")
        self.assertEqual(snapshot["weekly_reset"], "2d 14h")


class FailurePathTests(unittest.TestCase):
    def test_missing_token_is_explicit_error_without_fake_zero(self) -> None:
        missing = Path(__file__).resolve().parent / "no-such-credentials.json"
        snapshot = fetch.build_snapshot(env={}, creds_path=missing)
        self.assertFalse(snapshot["ok"])
        self.assertTrue(snapshot["error"])
        self.assertNotIn("session_remaining", snapshot)
        self.assertNotIn("weekly_remaining", snapshot)
        self.assertFalse(_has_remaining_zero(snapshot))

    def test_env_token_wins_over_missing_file(self) -> None:
        captured: dict = {}

        def opener(url, headers, timeout):
            captured["url"] = url
            captured["headers"] = dict(headers)
            body = (FIXTURES / "valid_usage.json").read_bytes()
            return 200, body

        missing = Path(__file__).resolve().parent / "no-such-credentials.json"
        snapshot = fetch.build_snapshot(
            env={"CLAUDE_CODE_OAUTH_TOKEN": "unit-test-token"},
            creds_path=missing,
            opener=opener,
            now=datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(snapshot["ok"])
        self.assertEqual(captured["url"], fetch.USAGE_URL)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer unit-test-token")
        self.assertEqual(captured["headers"]["anthropic-beta"], fetch.BETA_HEADER)
        self.assertEqual(snapshot["session_remaining"], 72.5)

    def test_http_401_is_explicit_error_without_fake_zero(self) -> None:
        def opener(url, headers, timeout):
            return 401, b'{"error":"unauthorized"}'

        snapshot = fetch.build_snapshot(
            env={"CLAUDE_CODE_OAUTH_TOKEN": "expired-token"},
            opener=opener,
        )
        self.assertFalse(snapshot["ok"])
        self.assertIn("expired", snapshot["error"].lower())
        self.assertNotIn("session_remaining", snapshot)
        self.assertFalse(_has_remaining_zero(snapshot))

    def test_http_429_is_explicit_error_without_fake_zero(self) -> None:
        def opener(url, headers, timeout):
            return 429, b'{"error":"rate limited"}'

        snapshot = fetch.build_snapshot(
            env={"CLAUDE_CODE_OAUTH_TOKEN": "valid-token"},
            opener=opener,
        )
        self.assertFalse(snapshot["ok"])
        self.assertIn("rate limited", snapshot["error"].lower())
        self.assertNotIn("session_remaining", snapshot)
        self.assertFalse(_has_remaining_zero(snapshot))

    def test_malformed_body_is_explicit_error_without_fake_zero(self) -> None:
        def opener(url, headers, timeout):
            return 200, (FIXTURES / "malformed_body.txt").read_bytes()

        snapshot = fetch.build_snapshot(
            env={"CLAUDE_CODE_OAUTH_TOKEN": "valid-token"},
            opener=opener,
        )
        self.assertFalse(snapshot["ok"])
        self.assertIn("malformed", snapshot["error"].lower())
        self.assertNotIn("session_remaining", snapshot)
        self.assertFalse(_has_remaining_zero(snapshot))

    def test_missing_windows_are_malformed_not_zero(self) -> None:
        snapshot = fetch.parse_usage({"unexpected": True})
        self.assertFalse(snapshot["ok"])
        self.assertTrue(snapshot["error"])
        self.assertNotIn("session_remaining", snapshot)


class TokenLookupTests(unittest.TestCase):
    def test_credentials_json_access_token(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            creds = Path(tmp) / "credentials.json"
            creds.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "file-token-value"}}),
                encoding="utf-8",
            )
            token = fetch.load_token(env={}, creds_path=creds)
            self.assertEqual(token, "file-token-value")
            env_token = fetch.load_token(
                env={"CLAUDE_CODE_OAUTH_TOKEN": "env-wins"},
                creds_path=creds,
            )
            self.assertEqual(env_token, "env-wins")


class SkinWiringTests(unittest.TestCase):
    def test_ini_binds_required_fields_and_shipped_fetch(self) -> None:
        ini = (SKIN_ROOT / "ClaudeUsage.ini").read_text(encoding="utf-8")
        self.assertIn("[Rainmeter]", ini)
        self.assertTrue(("fetch.py" in ini) or ("fetch.cmd" in ini), ini)
        self.assertIn("fetch.cmd", ini)
        self.assertIn("SessionUsed", ini)
        self.assertIn("WeeklyUsed", ini)
        lua = (RESOURCES / "parse.lua").read_text(encoding="utf-8")
        self.assertIn('extract_number(raw, "session_used")', lua)
        self.assertIn('extract_number(raw, "weekly_used")', lua)
        self.assertNotIn("session_remaining", lua)
        self.assertIn("SessionReset", ini)
        self.assertIn("WeeklyReset", ini)
        self.assertIn("#Error#", ini)
        self.assertIn("UpdateDivider=60", ini)
        self.assertIn("MeasureFetch", ini)
        self.assertIn("Plugin=RunCommand", ini)
        self.assertIn("Background=#@#Background.png", ini)
        self.assertIn("BackgroundMode=3", ini)
        self.assertIn("BackgroundMargins=0,34,0,14", ini)
        self.assertNotIn("Skins\\illustro", ini)
        self.assertNotIn("illustro\\", ini)
        self.assertTrue((RESOURCES / "Background.png").is_file())
        self.assertTrue((RESOURCES / "fetch.cmd").is_file())


class SecretHygieneTests(unittest.TestCase):
    """The shipped skin must not contain credentials or this machine's identity."""

    FORBIDDEN = (
        "sk-ant" + "-",
        "oat01" + "-",
        "ort01" + "-",
        "sato" + "_",
        "C:\\Users\\" + "sato" + "_",
    )

    def test_tree_has_no_tokens_or_local_account_paths(self) -> None:
        hits: list[str] = []
        shipped = [SKIN_ROOT / "ClaudeUsage.ini", *RESOURCES.iterdir()]
        for path in shipped:
            if not path.is_file():
                continue
            if path.suffix == ".pyc" or path.name == "snapshot.json":
                continue
            if path.suffix.lower() in {".png", ".bmp", ".jpg"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in self.FORBIDDEN:
                if needle in text:
                    hits.append(f"{path}: {needle}")
        self.assertEqual(hits, [])


class FetchUsageEntryTests(unittest.TestCase):
    def test_fetch_usage_uses_shipped_url_and_headers(self) -> None:
        captured: dict = {}

        def opener(url, headers, timeout):
            captured["url"] = url
            captured["headers"] = dict(headers)
            captured["timeout"] = timeout
            return 200, b"{}"

        status, body = fetch.fetch_usage("abc", opener=opener, timeout=7)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"{}")
        self.assertEqual(captured["url"], "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer abc")
        self.assertEqual(captured["headers"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(captured["timeout"], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
