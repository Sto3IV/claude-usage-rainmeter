"""Drive the shipped DeepSeek balance fetch/parse/format helpers."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

SKIN_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = SKIN_ROOT.parent / "@Resources" / "DeepSeek"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(RESOURCES))

import fetch  # noqa: E402


class ClampBalanceTests(unittest.TestCase):
    def test_clamp_valid(self) -> None:
        self.assertEqual(fetch.clamp_balance("16.24"), 16.24)
        self.assertEqual(fetch.clamp_balance(0), 0.0)
        self.assertEqual(fetch.clamp_balance(-5), 0.0)

    def test_clamp_invalid(self) -> None:
        with self.assertRaises(ValueError):
            fetch.clamp_balance("not_a_number")
        with self.assertRaises(ValueError):
            fetch.clamp_balance(float("nan"))
        with self.assertRaises(ValueError):
            fetch.clamp_balance(float("inf"))


class ParseBalanceTests(unittest.TestCase):
    def test_valid_usd_fixture(self) -> None:
        payload = json.loads((FIXTURES / "valid_balance.json").read_text(encoding="utf-8"))
        snapshot = fetch.parse_balance(payload, rate=6.67)
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["usd_balance"], 16.24)
        self.assertEqual(snapshot["usd_text"], "$16.24")
        self.assertEqual(snapshot["usd_sub"], "Top-up $16.24")
        self.assertAlmostEqual(snapshot["cny_balance"], round(16.24 * 6.67, 2), places=2)
        self.assertEqual(snapshot["cny_text"], f"¥{round(16.24 * 6.67, 2):.2f}")
        self.assertEqual(snapshot["cny_sub"], "Rate ~6.67")

    def test_dual_currency_payload(self) -> None:
        payload = {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "10.00", "topped_up_balance": "10.00", "granted_balance": "0.00"},
                {"currency": "CNY", "total_balance": "70.00", "topped_up_balance": "70.00", "granted_balance": "0.00"},
            ],
        }
        snapshot = fetch.parse_balance(payload, rate=6.67)
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["usd_balance"], 10.0)
        self.assertEqual(snapshot["usd_text"], "$10.00")
        self.assertEqual(snapshot["cny_balance"], 70.0)
        self.assertEqual(snapshot["cny_text"], "¥70.00")

    def test_cny_only_payload(self) -> None:
        payload = {
            "is_available": True,
            "balance_infos": [
                {"currency": "CNY", "total_balance": "66.70", "topped_up_balance": "66.70", "granted_balance": "0.00"}
            ],
        }
        snapshot = fetch.parse_balance(payload, rate=6.67)
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["cny_balance"], 66.70)
        self.assertEqual(snapshot["usd_balance"], 10.00)
        self.assertEqual(snapshot["usd_text"], "$10.00")
        self.assertEqual(snapshot["cny_text"], "¥66.70")

    def test_missing_balance_infos(self) -> None:
        snapshot = fetch.parse_balance({"is_available": True})
        self.assertFalse(snapshot["ok"])
        self.assertIn("missing balance_infos", snapshot["error"])

    def test_invalid_type(self) -> None:
        snapshot = fetch.parse_balance("not a dict")
        self.assertFalse(snapshot["ok"])


class HttpAndSnapshotTests(unittest.TestCase):
    def test_http_401(self) -> None:
        snapshot = fetch.snapshot_from_http(401, b"")
        self.assertFalse(snapshot["ok"])
        self.assertIn("Unauthorized", snapshot["error"])

    def test_http_429(self) -> None:
        snapshot = fetch.snapshot_from_http(429, b"")
        self.assertFalse(snapshot["ok"])
        self.assertEqual(snapshot["error"], "Rate limited")

    def test_http_500(self) -> None:
        snapshot = fetch.snapshot_from_http(500, b"")
        self.assertFalse(snapshot["ok"])
        self.assertIn("HTTP error 500", snapshot["error"])

    def test_malformed_json(self) -> None:
        snapshot = fetch.snapshot_from_http(200, (FIXTURES / "malformed_body.txt").read_bytes())
        self.assertFalse(snapshot["ok"])
        self.assertIn("Malformed JSON", snapshot["error"])


class TokenLookupTests(unittest.TestCase):
    def test_env_token_lookup(self) -> None:
        env = {"DEEPSEEK_API_KEY": "test_token_123"}
        self.assertEqual(fetch.load_token(env=env), "test_token_123")

    def test_creds_json_lookup(self) -> None:
        creds = FIXTURES / "temp_creds.json"
        try:
            creds.write_text(json.dumps({"api_key": "file_token_456"}), encoding="utf-8")
            self.assertEqual(fetch.load_token(env={}, creds_path=creds), "file_token_456")
        finally:
            if creds.exists():
                creds.unlink()


class CarryForwardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = FIXTURES / "temp_snapshot.json"
        self.GOOD = {
            "ok": True,
            "is_available": True,
            "usd_balance": 16.24,
            "usd_text": "$16.24",
            "usd_sub": "Top-up $16.24",
            "cny_balance": 108.34,
            "cny_text": "¥108.34",
            "cny_sub": "Rate ~6.67",
            "rate": 6.67,
            "error": "",
        }

    def tearDown(self) -> None:
        if self.target.exists():
            self.target.unlink()

    def test_carry_forward_keeps_previous_on_failure(self) -> None:
        self.target.write_text(json.dumps(self.GOOD), encoding="utf-8")
        failed = fetch.error_snapshot("HTTP error 500")
        merged = fetch.carry_forward(failed, self.target, 2000)
        self.assertTrue(merged["ok"])
        self.assertEqual(merged["usd_balance"], 16.24)
        self.assertEqual(merged["last_error"], "HTTP error 500")
        self.assertEqual(merged["checked_at"], 2000)

    def test_success_updates_both_stamps(self) -> None:
        merged = fetch.carry_forward(self.GOOD, self.target, 3000)
        self.assertEqual(merged["fetched_at"], 3000)
        self.assertEqual(merged["checked_at"], 3000)
        self.assertEqual(merged["last_error"], "")


def local_identity() -> tuple[str, ...]:
    candidates = [str(Path.home()), Path.home().name, os.environ.get("USERNAME", "")]
    return tuple(m for m in dict.fromkeys(candidates) if len(m) >= 4)


class SecretHygieneTests(unittest.TestCase):
    FORBIDDEN = (
        "sk-1b8c",
        "sk-ant",
    )

    def test_tree_has_no_tokens_or_local_account_paths(self) -> None:
        hits: list[str] = []
        shipped = [SKIN_ROOT / "DeepSeek.ini", *RESOURCES.iterdir()]
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


class SkinWiringTests(unittest.TestCase):
    def test_runcommand_timeout_is_milliseconds(self) -> None:
        text = (SKIN_ROOT / "DeepSeek.ini").read_text(encoding="utf-8")
        self.assertIn("Timeout=25000", text)
        self.assertIn('Parameter=/c ""#@#DeepSeek\\fetch.cmd""', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
