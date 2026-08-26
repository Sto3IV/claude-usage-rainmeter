"""Drive the shipped ClaudeUsage fetch/parse/format helpers."""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SKIN_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = SKIN_ROOT.parent / "@Resources" / "Claude"
SHARED_RESOURCES = SKIN_ROOT.parent / "@Resources"
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
        self.assertEqual(fetch.format_countdown("2099-01-01T09:00:00+00:00", now=now), "--")
        self.assertEqual(fetch.format_countdown("", now=now), "--")
        self.assertEqual(fetch.format_countdown(None, now=now), "--")

    def test_iso_to_unix_and_seconds_formatter(self) -> None:
        unix = fetch.iso_to_unix("2099-01-01T12:00:00+00:00")
        self.assertEqual(unix, int(datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(fetch.format_countdown_seconds(2 * 3600), "2h")
        self.assertEqual(fetch.format_countdown_seconds(2 * 86400 + 14 * 3600), "2d 14h")
        self.assertEqual(fetch.format_countdown_seconds(45 * 60), "45m")
        self.assertEqual(fetch.format_countdown_seconds(0), "--")
        self.assertEqual(fetch.format_countdown_seconds(-9000), "--")


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
        self.assertEqual(
            snapshot["session_reset_unix"],
            fetch.iso_to_unix(payload["five_hour"]["resets_at"]),
        )
        self.assertEqual(
            snapshot["weekly_reset_unix"],
            fetch.iso_to_unix(payload["seven_day"]["resets_at"]),
        )


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
        ini = (SKIN_ROOT / "Claude.ini").read_text(encoding="utf-8")
        self.assertIn("[Rainmeter]", ini)
        self.assertTrue(("fetch.py" in ini) or ("fetch.cmd" in ini), ini)
        self.assertIn("fetch.cmd", ini)
        self.assertIn("Program=cmd", ini)
        self.assertIn('Parameter=/c ""#@#Claude\\fetch.cmd""', ini)
        self.assertIn("SessionUsed", ini)
        self.assertIn("WeeklyUsed", ini)
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
        self.assertIn("FETCH_MAX", lua)
        # The backoff must key off checked_at, not off every Apply() read.
        self.assertIn("checkedAt ~= lastCheckedAt", lua)
        # 401 / "Credentials expired" must NOT climb the 429 ladder. One
        # expired token froze the bar on 73% for half an hour while the CLI
        # had already written a live token.
        self.assertIn('find("rate limit"', lua)
        self.assertIn("TickCountdowns", lua)
        self.assertIn("Plugin=RunCommand", ini)
        self.assertIn("[MeasureParse]", ini)
        self.assertIn("UpdateDivider=1", ini)
        self.assertNotIn("UpdateDivider=-1", ini)
        self.assertNotIn("[MeasureTimer]", ini)
        self.assertIn("Background=#@#Background.png", ini)
        self.assertIn("BackgroundMode=3", ini)
        self.assertIn("BackgroundMargins=0,34,0,14", ini)
        self.assertNotIn("Skins\\illustro", ini)
        self.assertNotIn("illustro\\", ini)
        self.assertTrue((SHARED_RESOURCES / "Background.png").is_file())
        self.assertTrue((RESOURCES / "fetch.cmd").is_file())
        self.assertIn("ScriptFile=#@#Claude\\parse.lua", ini)

    def test_runcommand_timeout_is_milliseconds_not_seconds(self) -> None:
        """RunCommand's Timeout is MILLISECONDS, and State=Hide makes it KILL.

        This shipped as Timeout=25, so Rainmeter killed cmd.exe 25ms after
        launch -- far too early for it to even spawn Python. fetch.py never ran,
        snapshot.json never changed, and the skin sat on day-old numbers while
        FinishAction kept firing as though everything were healthy.
        """
        ini = (SKIN_ROOT / "Claude.ini").read_text(encoding="utf-8")
        found = re.search(r"^Timeout=(\d+)", ini, re.MULTILINE)
        self.assertIsNotNone(found, "MeasureFetch must set an explicit Timeout")
        assert found is not None
        self.assertGreaterEqual(
            int(found.group(1)),
            15000,
            "Timeout is in milliseconds and must clear fetch.py's own 10s HTTP timeout",
        )


class CarryForwardTests(unittest.TestCase):
    """A failed refresh must never blank a good reading.

    /api/oauth/usage 429s readily. Before this, any rejection overwrote the
    snapshot with a bare error object and the skin lost every value it had.
    """

    GOOD = {
        "ok": True,
        "session_used": 60.0,
        "weekly_used": 16.0,
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

    def _write(self, payload: dict) -> None:
        self.target.write_text(json.dumps(payload), encoding="utf-8")

    def test_failure_keeps_previous_values_and_fetched_at(self) -> None:
        self._write(self.GOOD)
        merged = fetch.carry_forward(fetch.error_snapshot("Rate limited"), self.target, 2000)

        self.assertTrue(merged["ok"])
        self.assertEqual(merged["session_used"], 60.0)
        self.assertEqual(merged["weekly_used"], 16.0)
        self.assertEqual(merged["session_reset_unix"], 1786955399)
        self.assertEqual(merged["fetched_at"], 1000, "data did not get any newer")
        self.assertEqual(merged["checked_at"], 2000, "but we did just try")
        self.assertEqual(merged["last_error"], "Rate limited")

    def test_success_stamps_both_and_clears_error(self) -> None:
        self._write({**self.GOOD, "last_error": "Rate limited"})
        fresh = {"ok": True, "session_used": 61.0, "weekly_used": 17.0, "error": ""}
        merged = fetch.carry_forward(fresh, self.target, 3000)

        self.assertEqual(merged["session_used"], 61.0)
        self.assertEqual(merged["fetched_at"], 3000)
        self.assertEqual(merged["checked_at"], 3000)
        self.assertEqual(merged["last_error"], "")

    def test_failure_without_usable_prior_stays_not_ok(self) -> None:
        for label, prepare in (
            ("missing file", lambda: None),
            ("unparseable file", lambda: self.target.write_text("{oh no", encoding="utf-8")),
            ("prior also failed", lambda: self._write({"ok": False, "error": "boom"})),
            ("prior is not an object", lambda: self.target.write_text("[]", encoding="utf-8")),
        ):
            with self.subTest(prior=label):
                if self.target.exists():
                    self.target.unlink()
                prepare()
                merged = fetch.carry_forward(
                    fetch.error_snapshot("Rate limited"), self.target, 4000
                )
                self.assertFalse(merged["ok"])
                self.assertEqual(merged["last_error"], "Rate limited")
                self.assertEqual(merged["checked_at"], 4000)
                self.assertFalse(_has_remaining_zero(merged))

    def test_round_trip_through_write_and_read(self) -> None:
        fetch.write_snapshot(self.GOOD, self.target)
        self.assertEqual(fetch.read_snapshot(self.target), self.GOOD)


class OAuthRefreshTests(unittest.TestCase):
    """A 401 is usually an expired access token, not a missing login."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.creds = Path(self._tmp.name) / "credentials.json"
        self.creds.write_text(
            json.dumps(
                {
                    "mcpOAuth": {"keep": True},
                    "claudeAiOauth": {
                        "accessToken": "old-access",
                        "refreshToken": "old-refresh",
                        "expiresAt": 1,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.usage = json.dumps(
            {
                "five_hour": {
                    "utilization": 100,
                    "resets_at": "2099-01-01T12:00:00+00:00",
                },
                "seven_day": {
                    "utilization": 62,
                    "resets_at": "2099-01-04T00:00:00+00:00",
                },
            }
        ).encode()

    def test_401_retries_after_refresh(self) -> None:
        gets = {"n": 0}

        def opener(url, headers, timeout):
            gets["n"] += 1
            if gets["n"] == 1:
                return 401, b"{}"
            auth = headers.get("Authorization") or ""
            self.assertTrue(auth.endswith("new-access"), auth)
            return 200, self.usage

        def poster(url, headers, timeout, data):
            payload = json.loads(data.decode("utf-8"))
            self.assertEqual(payload["grant_type"], "refresh_token")
            self.assertEqual(payload["refresh_token"], "old-refresh")
            self.assertIn("/v1/oauth/token", url)
            return 200, json.dumps(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                }
            ).encode()

        snap = fetch.build_snapshot(
            env={},
            creds_path=self.creds,
            opener=opener,
            poster=poster,
            now=datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["session_used"], 100.0)
        self.assertEqual(snap["weekly_used"], 62.0)
        self.assertEqual(gets["n"], 2)
        stored = json.loads(self.creds.read_text(encoding="utf-8"))
        self.assertEqual(stored["claudeAiOauth"]["accessToken"], "new-access")
        self.assertEqual(stored["claudeAiOauth"]["refreshToken"], "new-refresh")
        self.assertTrue(stored["mcpOAuth"]["keep"], "sibling keys must survive write-back")

    def test_401_without_refresh_token_stays_expired(self) -> None:
        self.creds.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "old-access"}}),
            encoding="utf-8",
        )

        def opener(url, headers, timeout):
            return 401, b"{}"

        def poster(url, headers, timeout, data):
            raise AssertionError("must not POST without a refresh token")

        snap = fetch.build_snapshot(
            env={}, creds_path=self.creds, opener=opener, poster=poster
        )
        self.assertFalse(snap["ok"])
        self.assertIn("expired", snap["error"].lower())

    def test_env_token_does_not_touch_the_file(self) -> None:
        def opener(url, headers, timeout):
            return 401, b"{}"

        def poster(url, headers, timeout, data):
            raise AssertionError("env token has no file refresh handle")

        snap = fetch.build_snapshot(
            env={"CLAUDE_CODE_OAUTH_TOKEN": "env-only"},
            creds_path=self.creds,
            opener=opener,
            poster=poster,
        )
        self.assertFalse(snap["ok"])
        stored = json.loads(self.creds.read_text(encoding="utf-8"))
        self.assertEqual(stored["claudeAiOauth"]["accessToken"], "old-access")


def local_identity() -> tuple[str, ...]:
    """Markers for whoever is running this, derived at runtime.

    Hardcoding a username would do the wrong thing twice: it would miss every
    other contributor's paths, and it would publish the author's own account
    name to anyone reading the repo. Short values are dropped so a two-letter
    username cannot match half the tree.
    """
    candidates = [str(Path.home()), Path.home().name, os.environ.get("USERNAME", "")]
    return tuple(m for m in dict.fromkeys(candidates) if len(m) >= 4)


class SecretHygieneTests(unittest.TestCase):
    """The shipped skin must not contain credentials or this machine's identity."""

    FORBIDDEN = (
        "sk-ant" + "-",
        "oat01" + "-",
        "ort01" + "-",
    )

    def test_tree_has_no_tokens_or_local_account_paths(self) -> None:
        hits: list[str] = []
        shipped = [SKIN_ROOT / "Claude.ini", *RESOURCES.iterdir()]
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
