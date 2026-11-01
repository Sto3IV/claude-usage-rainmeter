"""DeepSeek API balance and credits fetch for Rainmeter (Before Dawn RM Bar and AIUsageLimits).

Fetches account balance from https://api.deepseek.com/user/balance.
Presents USD credits (topped up) on top and CNY credits (granted/gifted) on bottom.
DeepSeek maintains two separate balances:
  - USD balance (user top-up credits)
  - CNY balance (maintenance compensations and granted gifts)

Token lookup:
  1. DEEPSEEK_API_KEY environment variable
  2. ~/.deepseek/credentials.json -> api_key

Maintains the carry_forward contract established by Grok and Claude:
a temporary network failure preserves the last known good reading rather
than flashing a blank or 0 balance.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BALANCE_URL = "https://api.deepseek.com/user/balance"
USER_AGENT = "rainmeter-deepseek-usage"
DEFAULT_CREDS = Path.home() / ".deepseek" / "credentials.json"

Opener = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


def clamp_balance(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("balance is not a valid number")
    if math.isnan(number) or math.isinf(number):
        raise ValueError("balance is not a finite number")
    return max(0.0, number)


def load_token(
    env: Optional[Mapping[str, str]] = None,
    creds_path: Optional[os.PathLike[str] | str] = None,
) -> Optional[str]:
    environ = os.environ if env is None else env
    env_tok = (environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_tok:
        return env_tok
    path = Path(creds_path) if creds_path is not None else DEFAULT_CREDS
    if not path.is_file():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        token = blob.get("api_key") or blob.get("token") or blob.get("key")
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    token = (token or "").strip()
    return token or None


def error_snapshot(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _http_exchange(request: Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.getcode()), response.read(65536)
    except HTTPError as exc:
        body = b""
        try:
            body = exc.read(65536)
        except OSError:
            body = b""
        return int(exc.code), body


def default_opener(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    return _http_exchange(Request(url, headers=dict(headers), method="GET"), timeout)


def fetch_balance(
    token: str,
    opener: Optional[Opener] = None,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    transport = opener or default_opener
    return transport(BALANCE_URL, headers, timeout)


def parse_balance(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return error_snapshot("Unexpected /user/balance payload")

    is_available = bool(payload.get("is_available", False))
    balance_infos = payload.get("balance_infos")
    if not isinstance(balance_infos, list):
        return error_snapshot("Malformed /user/balance body: missing balance_infos")

    usd_info: Optional[dict[str, Any]] = None
    cny_info: Optional[dict[str, Any]] = None

    for item in balance_infos:
        if not isinstance(item, dict):
            continue
        cur = str(item.get("currency") or "").upper()
        if cur == "USD":
            usd_info = item
        elif cur == "CNY":
            cny_info = item

    if usd_info is None and cny_info is None:
        return error_snapshot("No USD or CNY balance info in response")

    usd_total = 0.0
    usd_topped_up = 0.0
    usd_granted = 0.0
    cny_total = 0.0
    cny_topped_up = 0.0
    cny_granted = 0.0

    if usd_info is not None:
        try:
            usd_total = clamp_balance(usd_info.get("total_balance", 0.0))
            usd_topped_up = clamp_balance(usd_info.get("topped_up_balance", 0.0))
            usd_granted = clamp_balance(usd_info.get("granted_balance", 0.0))
        except ValueError:
            return error_snapshot("Malformed USD balance numbers")

    if cny_info is not None:
        try:
            cny_total = clamp_balance(cny_info.get("total_balance", 0.0))
            cny_topped_up = clamp_balance(cny_info.get("topped_up_balance", 0.0))
            cny_granted = clamp_balance(cny_info.get("granted_balance", 0.0))
        except ValueError:
            return error_snapshot("Malformed CNY balance numbers")

    usd_sub = f"Top-up ${usd_topped_up:.2f}" if usd_granted == 0 else f"Grant ${usd_granted:.2f}"
    cny_sub = f"{cny_granted:.2f}" if cny_granted > 0 else f"{cny_total:.2f}"

    return {
        "ok": True,
        "is_available": is_available,
        "usd_balance": usd_total,
        "usd_text": f"{usd_total:.2f}",
        "usd_sub": usd_sub,
        "cny_balance": cny_total,
        "cny_text": f"{cny_total:.2f}",
        "cny_sub": cny_sub,
        "cny_granted": cny_granted,
        "cny_topped_up": cny_topped_up,
        "error": "",
    }


def snapshot_from_http(
    status: int,
    body: Any,
) -> dict[str, Any]:
    if status == 401:
        return error_snapshot("Unauthorized -- check DEEPSEEK_API_KEY")
    if status == 429:
        return error_snapshot("Rate limited")
    if status != 200:
        return error_snapshot(f"HTTP error {status}")
    if isinstance(body, bytes):
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return error_snapshot("Malformed JSON response from /user/balance")
    elif isinstance(body, dict):
        payload = body
    else:
        return error_snapshot("Malformed body from /user/balance")
    return parse_balance(payload)


def read_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_snapshot(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def carry_forward(
    prev: dict[str, Any],
    fresh: dict[str, Any],
    now: int,
) -> dict[str, Any]:
    if fresh.get("ok"):
        out = dict(fresh)
        out["fetched_at"] = now
        out["checked_at"] = now
        out["last_error"] = ""
        return out

    if prev and prev.get("ok"):
        out = dict(prev)
        out["checked_at"] = now
        out["last_error"] = fresh.get("error") or "Fetch failed"
        return out

    out = dict(fresh)
    out["fetched_at"] = now
    out["checked_at"] = now
    out["last_error"] = fresh.get("error") or "Fetch failed"
    return out


def run(
    snapshot_path: Optional[os.PathLike[str] | str] = None,
    env: Optional[Mapping[str, str]] = None,
    opener: Optional[Opener] = None,
    now: Optional[int] = None,
) -> int:
    # Ensure stdout handles output gracefully
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    out_path = Path(snapshot_path) if snapshot_path else Path(__file__).resolve().parent / "snapshot.json"
    now_ts = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    prev = read_snapshot(out_path)

    token = load_token(env=env)
    if not token:
        snap = carry_forward(prev, error_snapshot("Missing DEEPSEEK_API_KEY"), now_ts)
        write_snapshot(out_path, snap)
        print(json.dumps(snap, indent=2))
        return 1

    try:
        status, body = fetch_balance(token, opener=opener)
    except URLError as exc:
        snap = carry_forward(prev, error_snapshot(f"Network error: {exc.reason}"), now_ts)
        write_snapshot(out_path, snap)
        print(json.dumps(snap, indent=2))
        return 1
    except OSError as exc:
        snap = carry_forward(prev, error_snapshot(f"Network error: {exc}"), now_ts)
        write_snapshot(out_path, snap)
        print(json.dumps(snap, indent=2))
        return 1

    fresh = snapshot_from_http(status, body)
    snap = carry_forward(prev, fresh, now_ts)
    write_snapshot(out_path, snap)

    try:
        print(json.dumps(snap, indent=2))
    except UnicodeEncodeError:
        print(json.dumps(snap, indent=2).encode("ascii", errors="replace").decode("ascii"))
    return 0 if snap.get("ok") else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch DeepSeek balance snapshot for Rainmeter")
    parser.add_argument("--snapshot", type=Path, default=None, help="Path to write snapshot.json")
    args = parser.parse_args()
    code = run(snapshot_path=args.snapshot)
    sys.exit(code)


if __name__ == "__main__":
    main()
