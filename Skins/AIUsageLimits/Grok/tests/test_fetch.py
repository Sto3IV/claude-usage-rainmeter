"""Drive the shipped Grok weekly fetch/parse helpers."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SKIN_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = SKIN_ROOT.parent / "@Resources" / "Grok"
SHARED_RESOURCES = SKIN_ROOT.parent / "@Resources"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(RESOURCES))

import fetch  # noqa: E402


class ParseBillingTests(unittest.TestCase):
    def test_valid_weekly_fixture(self) -> None:
        payload = json.loads((FIXTURES / "valid_billing.json").read_text(encoding="utf-8"))
        now = datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc)
        snap = fetch.parse_billing(payload, now=now)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["error"], "")
        self.assertEqual(snap["weekly_used"], 12.0)
        self.assertEqual(snap["weekly_remaining"], 88.0)
        self.assertEqual(snap["weekly_reset"], "7d")
        self.assertEqual(snap["subscription_tier"], "SuperGrok")
        self.assertEqual(
            snap["weekly_reset_unix"],
            fetch.iso_to_unix(payload["config"]["currentPeriod"]["end"]),
        )

    def test_flat_config_without_wrapper(self) -> None:
        snap = fetch.parse_billing(
            {
                "creditUsagePercent": 50,
                "billingPeriodEnd": "2099-01-02T00:00:00+00:00",
            },
            now=datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["weekly_used"], 50.0)
        self.assertEqual(snap["weekly_reset"], "1d")

    def test_malformed_is_error_not_zero(self) -> None:
        snap = fetch.parse_billing({"unexpected": True})
        self.assertFalse(snap["ok"])
        self.assertTrue(snap["error"])
        self.assertNotIn("weekly_used", snap)


class HttpAndLogTests(unittest.TestCase):
    def test_http_401_is_explicit(self) -> None:
        snap = fetch.snapshot_from_http(401, b"{}")
        self.assertFalse(snap["ok"])
        self.assertIn("expired", snap["error"].lower())
        self.assertNotIn("weekly_used", snap)

    def test_http_429_is_explicit(self) -> None:
        snap = fetch.snapshot_from_http(429, b"{}")
        self.assertFalse(snap["ok"])
        self.assertIn("rate limited", snap["error"].lower())
        self.assertNotIn("weekly_used", snap)

    def test_malformed_body(self) -> None:
        snap = fetch.snapshot_from_http(200, (FIXTURES / "malformed_body.txt").read_bytes())
        self.assertFalse(snap["ok"])
        self.assertIn("malformed", snap["error"].lower())

    def test_log_uses_latest_event(self) -> None:
        older = {
            "msg": "billing: fetched credits config",
            "ctx": {
                "config": {
                    "creditUsagePercent": 12.0,
                    "currentPeriod": {"end": "2099-01-08T00:00:00+00:00"},
                }
            },
        }
        newer = {
            "msg": "billing: fetched credits config",
            "ctx": {
                "config": {
                    "creditUsagePercent": 22.0,
                    "currentPeriod": {"end": "2099-01-08T00:00:00+00:00"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unified.jsonl"
            log.write_text(json.dumps(older) + "\n" + json.dumps(newer) + "\n", encoding="utf-8")
            snap = fetch.read_log_billing(log, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["weekly_used"], 22.0)

    def test_log_fallback(self) -> None:
        payload = json.loads((FIXTURES / "valid_billing.json").read_text(encoding="utf-8"))
        line = json.dumps({"msg": "billing: fetched credits config", "ctx": payload})
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unified.jsonl"
            log.write_text("not json\n" + line + "\n", encoding="utf-8")
            snap = fetch.read_log_billing(
                log,
                now=datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc),
            )
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["weekly_used"], 12.0)
        self.assertEqual(snap["source"], "log")

    def test_missing_auth_and_log(self) -> None:
        missing = Path(__file__).resolve().parent / "no-such-auth.json"
        missing_log = Path(__file__).resolve().parent / "no-such-log.jsonl"
        snap = fetch.build_snapshot(auth_path=missing, log_path=missing_log)
        self.assertFalse(snap["ok"])
        self.assertTrue(snap["error"])
        self.assertNotIn("weekly_used", snap)

    def test_live_http_wins_over_log(self) -> None:
        """HTTP is the primary source; the log never shadows a live answer."""
        live = json.dumps({
            "creditUsagePercent": 33,
            "currentPeriod": {"end": "2099-01-04T00:00:00+00:00"},
        }).encode()

        def opener(url, headers, timeout):
            return 200, live

        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(
                json.dumps({"https://auth.x.ai::x": {"key": "unit-token", "user_id": "u"}}),
                encoding="utf-8",
            )
            log = Path(tmp) / "unified.jsonl"
            stale = {
                "msg": "billing: fetched credits config",
                "ctx": json.loads((FIXTURES / "valid_billing.json").read_text(encoding="utf-8")),
            }
            log.write_text(json.dumps(stale) + "\n", encoding="utf-8")
            snap = fetch.build_snapshot(
                auth_path=auth,
                log_path=log,
                opener=opener,
                now=datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc),
            )
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["weekly_used"], 33.0)
        self.assertEqual(snap["source"], "http")
        self.assertEqual(snap["weekly_reset"], "3d")

    def test_billing_url_carries_the_v1_prefix(self) -> None:
        """The route only exists under /v1.

        Without it cli-chat-proxy answers 404, and that 404 is what silently
        demoted the whole fetcher to scraping an hours-old log line.
        """
        self.assertTrue(fetch.BILLING_URLS, "nothing to try")
        for url in fetch.BILLING_URLS:
            with self.subTest(url=url):
                self.assertIn("/v1/billing?format=credits", url)
                self.assertNotRegex(url, r"\.com/billing", "the /v1 went missing")

    def test_log_is_the_fallback_when_http_fails(self) -> None:
        """An unreachable network keeps a real number on the panel."""

        def opener(url, headers, timeout):
            raise OSError("network down")

        snap = self._build_with(opener)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["weekly_used"], 12.0)
        self.assertEqual(snap["source"], "log")
        self.assertTrue(snap.get("last_error"), "HTTP failure must still arm backoff")

    def test_log_rescues_a_non_200(self) -> None:
        """A dead token leaves the last real number visible, not a blank panel.

        ok stays true so the percent remains, but last_error carries the 401
        so backoff fires instead of polling a dead token every cycle.
        """

        def opener(url, headers, timeout):
            return 401, b"{}"

        snap = self._build_with(opener)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["source"], "log")
        self.assertIn("expired", str(snap.get("last_error") or "").lower())

    def test_opener_receives_http_timeout(self) -> None:
        seen: list[float] = []

        def opener(url, headers, timeout):
            seen.append(timeout)
            raise OSError("network down")

        self._build_with(opener)
        self.assertEqual(seen, [fetch.HTTP_TIMEOUT])
        self.assertGreaterEqual(fetch.HTTP_TIMEOUT, 8.0)

    def test_log_finds_billing_under_trailing_noise(self) -> None:
        event = {
            "ts": "2026-08-23T17:00:58.526Z",
            "msg": "billing: fetched credits config",
            "ctx": json.loads((FIXTURES / "valid_billing.json").read_text(encoding="utf-8")),
        }
        noise = "\n".join("{}" for _ in range(4000))
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unified.jsonl"
            log.write_text(json.dumps(event) + "\n" + noise + "\n", encoding="utf-8")
            snap = fetch.read_log_billing(log, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["weekly_used"], 12.0)
        self.assertEqual(snap["data_ts"], fetch.iso_to_unix(event["ts"]))

    def test_log_reports_when_its_line_was_written(self) -> None:
        """The line's own ts, not the moment we happened to read it.

        The log only moves when the CLI refetches billing, so reading it says
        nothing about how current the percent is. data_ts is what lets
        carry_forward stamp an honest fetched_at.
        """
        event = {
            "ts": "2026-08-23T14:41:11.845Z",
            "msg": "billing: fetched credits config",
            "ctx": json.loads((FIXTURES / "valid_billing.json").read_text(encoding="utf-8")),
        }
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unified.jsonl"
            log.write_text(json.dumps(event) + "\n", encoding="utf-8")
            snap = fetch.read_log_billing(log, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(snap["data_ts"], fetch.iso_to_unix(event["ts"]))

    def test_log_without_a_timestamp_is_still_usable(self) -> None:
        event = {
            "msg": "billing: fetched credits config",
            "ctx": json.loads((FIXTURES / "valid_billing.json").read_text(encoding="utf-8")),
        }
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unified.jsonl"
            log.write_text(json.dumps(event) + "\n", encoding="utf-8")
            snap = fetch.read_log_billing(log, now=datetime(2099, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["data_ts"], 0, "unknown age falls back to now downstream")

    def _build_with(self, opener) -> dict:
        """build_snapshot over a valid token and a valid log, with HTTP steered."""
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(
                json.dumps({"https://auth.x.ai::x": {"key": "unit-token", "user_id": "u"}}),
                encoding="utf-8",
            )
            log = Path(tmp) / "unified.jsonl"
            event = {
                "msg": "billing: fetched credits config",
                "ctx": json.loads((FIXTURES / "valid_billing.json").read_text(encoding="utf-8")),
            }
            log.write_text(json.dumps(event) + "\n", encoding="utf-8")
            return fetch.build_snapshot(
                auth_path=auth,
                log_path=log,
                opener=opener,
                now=datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc),
            )


class CarryForwardTests(unittest.TestCase):
    """A failed refresh must never blank a good reading.

    Before this, any failure overwrote the snapshot with a bare error object and
    the skin lost every value it had.
    """

    GOOD = {
        "ok": True,
        "weekly_used": 26.0,
        "weekly_reset_unix": 1787052664,
        "error": "",
        "fetched_at": 1000,
        "checked_at": 1000,
        "last_error": "",
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "snapshot.json"

    def test_failure_keeps_previous_values_and_fetched_at(self) -> None:
        self.target.write_text(json.dumps(self.GOOD), encoding="utf-8")
        merged = fetch.carry_forward(fetch.error_snapshot("no log"), self.target, 2000)
        self.assertTrue(merged["ok"])
        self.assertEqual(merged["weekly_used"], 26.0)
        self.assertEqual(merged["weekly_reset_unix"], 1787052664)
        self.assertEqual(merged["fetched_at"], 1000, "data did not get any newer")
        self.assertEqual(merged["checked_at"], 2000, "but we did just try")
        self.assertEqual(merged["last_error"], "no log")

    def test_success_stamps_both_and_clears_error(self) -> None:
        self.target.write_text(
            json.dumps({**self.GOOD, "last_error": "no log"}), encoding="utf-8"
        )
        merged = fetch.carry_forward({"ok": True, "weekly_used": 27.0}, self.target, 3000)
        self.assertEqual(merged["weekly_used"], 27.0)
        self.assertEqual(merged["fetched_at"], 3000)
        self.assertEqual(merged["checked_at"], 3000)
        self.assertEqual(merged["last_error"], "")

    def test_log_rescue_preserves_http_error_through_carry_forward(self) -> None:
        merged = fetch.carry_forward(
            {
                "ok": True,
                "weekly_used": 12.0,
                "source": "log",
                "data_ts": 1500,
                "last_error": "Rate limited",
            },
            self.target,
            9000,
        )
        self.assertEqual(merged["fetched_at"], 1500)
        self.assertEqual(merged["last_error"], "Rate limited")

    def test_log_timestamp_becomes_fetched_at(self) -> None:
        """fetched_at means "when the data was true", and the log knows.

        Stamping now() over a line the CLI wrote hours ago is what left the bar's
        staleness indicator structurally unable to fire for Grok.
        """
        merged = fetch.carry_forward(
            {"ok": True, "weekly_used": 12.0, "data_ts": 1500}, self.target, 9000
        )
        self.assertEqual(merged["fetched_at"], 1500, "when the CLI wrote the line")
        self.assertEqual(merged["checked_at"], 9000, "when we read it")
        self.assertNotIn("data_ts", merged, "internal; never reaches snapshot.json")

    def test_source_without_a_timestamp_stamps_now(self) -> None:
        for label, fresh in (
            ("live http", {"ok": True, "weekly_used": 12.0}),
            ("log with unreadable ts", {"ok": True, "weekly_used": 12.0, "data_ts": 0}),
        ):
            with self.subTest(source=label):
                merged = fetch.carry_forward(fresh, self.target, 9000)
                self.assertEqual(merged["fetched_at"], 9000)

    def test_older_log_does_not_clobber_newer_snapshot(self) -> None:
        """One HTTP blip used to rewind the panel to the last CLI scrape."""
        self.target.write_text(
            json.dumps(
                {
                    **self.GOOD,
                    "weekly_used": 30.0,
                    "source": "http",
                    "fetched_at": 5000,
                    "checked_at": 5000,
                }
            ),
            encoding="utf-8",
        )
        merged = fetch.carry_forward(
            {
                "ok": True,
                "weekly_used": 20.0,
                "source": "log",
                "data_ts": 1000,
                "last_error": "Grok billing timed out",
            },
            self.target,
            9000,
        )
        self.assertEqual(merged["weekly_used"], 30.0)
        self.assertEqual(merged["source"], "http")
        self.assertEqual(merged["fetched_at"], 5000)
        self.assertEqual(merged["checked_at"], 9000)
        self.assertEqual(merged["last_error"], "Grok billing timed out")

    def test_newer_log_is_allowed_to_replace(self) -> None:
        self.target.write_text(
            json.dumps({**self.GOOD, "weekly_used": 20.0, "source": "log", "fetched_at": 1000}),
            encoding="utf-8",
        )
        merged = fetch.carry_forward(
            {
                "ok": True,
                "weekly_used": 30.0,
                "source": "log",
                "data_ts": 2000,
                "last_error": "Grok billing request failed",
            },
            self.target,
            9000,
        )
        self.assertEqual(merged["weekly_used"], 30.0)
        self.assertEqual(merged["source"], "log")
        self.assertEqual(merged["fetched_at"], 2000)
        self.assertEqual(merged["last_error"], "Grok billing request failed")

    def test_http_success_clears_last_error_from_a_log_rescue(self) -> None:
        self.target.write_text(
            json.dumps({**self.GOOD, "last_error": "Grok billing timed out"}),
            encoding="utf-8",
        )
        merged = fetch.carry_forward(
            {"ok": True, "weekly_used": 31.0, "source": "http"},
            self.target,
            9000,
        )
        self.assertEqual(merged["weekly_used"], 31.0)
        self.assertEqual(merged["source"], "http")
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
                merged = fetch.carry_forward(fetch.error_snapshot("no log"), self.target, 4000)
                self.assertFalse(merged["ok"])
                self.assertEqual(merged["last_error"], "no log")
                self.assertNotIn("weekly_used", merged)


class SkinWiringTests(unittest.TestCase):
    def test_ini_is_weekly_only_and_invokes_fetch(self) -> None:
        ini = (SKIN_ROOT / "Grok.ini").read_text(encoding="utf-8")
        self.assertIn("[Rainmeter]", ini)
        self.assertIn("fetch.cmd", ini)
        self.assertIn("cmd.exe", ini)
        self.assertIn("WeeklyUsed", ini)
        self.assertIn("WeeklyReset", ini)
        self.assertIn("#Error#", ini)
        self.assertNotIn("SessionUsed", ini)
        self.assertNotIn("Session (5h)", ini)
        self.assertIn("UpdateDivider=1", ini)
        lua = (RESOURCES / "parse.lua").read_text(encoding="utf-8")
        self.assertIn("APPLY_EVERY = 5", lua)
        self.assertIn("FETCH_MAX", lua)
        # The backoff must key off checked_at, not off every Apply() read.
        self.assertIn("checkedAt ~= lastCheckedAt", lua)
        self.assertIn('find("rate limit"', lua)
        self.assertIn("rateLimited", lua)
        self.assertIn('lastError == "" or rateLimited', lua)
        self.assertIn("weekly_used", lua)
        self.assertNotIn("session_used", lua)
        self.assertTrue((SHARED_RESOURCES / "Background.png").is_file())
        self.assertTrue((RESOURCES / "fetch.cmd").is_file())
        self.assertIn("Background=#@#Background.png", ini)
        self.assertIn("ScriptFile=#@#Grok\\parse.lua", ini)

    def test_fetch_cadence_is_remote_safe(self) -> None:
        """fetch.py stopped scraping a local log and now calls cli-chat-proxy.

        60s was justified by "reads a local .jsonl in ~0.4s" -- true then, wrong
        now. Claude's endpoint answered three 429s in four at that rate; this
        one's limit is unmeasured, so the cadence matches the other two skins
        rather than guessing low. FETCH_LAPSED is applied directly here, not
        through a min() as in the bar's aiusage.lua, so it IS the cadence for as
        long as a reset window sits lapsed -- it has to clear the floor too.
        """
        lua = (RESOURCES / "parse.lua").read_text(encoding="utf-8")
        for name, floor in (("FETCH_EVERY", 300), ("FETCH_LAPSED", 120)):
            with self.subTest(constant=name):
                found = re.search(rf"^{name} = (\d+)", lua, re.MULTILINE)
                self.assertIsNotNone(found, f"{name} missing from parse.lua")
                self.assertGreaterEqual(int(found.group(1)), floor)

    def test_runcommand_timeout_is_milliseconds_not_seconds(self) -> None:
        """RunCommand's Timeout is MILLISECONDS, and State=Hide makes it KILL.

        This shipped as Timeout=25, so Rainmeter killed cmd.exe 25ms after
        launch -- far too early for it to even spawn Python. fetch.py never ran,
        snapshot.json never changed, and the skin sat on day-old numbers while
        FinishAction kept firing as though everything were healthy.
        """
        ini = (SKIN_ROOT / "Grok.ini").read_text(encoding="utf-8")
        found = re.search(r"^Timeout=(\d+)", ini, re.MULTILINE)
        self.assertIsNotNone(found, "MeasureFetch must set an explicit Timeout")
        assert found is not None
        self.assertGreaterEqual(
            int(found.group(1)),
            15000,
            "Timeout is in milliseconds and must clear the fetcher's own HTTP timeout",
        )


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
    FORBIDDEN = (
        "sk-ant" + "-",
        "eyJ" + "0eXAi",
    )

    def test_shipped_files_have_no_tokens(self) -> None:
        hits = []
        shipped = [SKIN_ROOT / "Grok.ini", *RESOURCES.iterdir()]
        for path in shipped:
            if not path.is_file() or path.suffix.lower() in {".png", ".pyc"}:
                continue
            if path.name == "snapshot.json":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in self.FORBIDDEN + local_identity():
                if needle in text:
                    hits.append(f"{path}: {needle}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
