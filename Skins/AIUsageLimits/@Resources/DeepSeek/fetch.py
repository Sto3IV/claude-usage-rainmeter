"""DeepSeek API balance and credits fetch for Rainmeter (Before Dawn RM Bar and AIUsageLimits).

Fetches account balance from https://api.deepseek.com/user/balance.
Presents USD credits on top and CNY credits on the bottom, with automatic
conversion when only one currency is present on the account.

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
EXCHANGE_URL = "https://open.er-api.com/v6/latest/USD"
DEFAULT_RATE = 6.67
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


def fetch_exchange_rate(opener: Optional[Opener] = None, timeout: float = 3.0) -> float:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    transport = opener or default_opener
    try:
        status, body = transport(EXCHANGE_URL, headers, timeout)
        if status == 200:
            data = json.loads(body.decode("utf-8", errors="replace"))
            rate = float(data.get("rates", {}).get("CNY", 0.0))
            if rate > 0:
                return rate
    except Exception:
        pass
    return DEFAULT_RATE


def parse_balance(payload: Any, rate: float = DEFAULT_RATE) -> dict[str, Any]:
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

    # If only USD was provided, calculate CNY using the exchange rate
    if usd_info is not None and cny_info is None:
        cny_total = round(usd_total * rate, 2)
        cny_topped_up = round(usd_topped_up * rate, 2)
        cny_granted = round(usd_granted * rate, 2)
        cny_sub = f"Rate ~{rate:.2f}"
        if usd_granted > 0:
            usd_sub = f"Grant ${usd_granted:.2f}"
        else:
            usd_sub = f"Top-up ${usd_topped_up:.2f}"
    # If only CNY was provided, calculate USD using the exchange rate
    elif cny_info is not None and usd_info is None:
        usd_total = round(cny_total / rate, 2)
        usd_topped_up = round(cny_topped_up / rate, 2)
        usd_granted = round(cny_granted / rate, 2)
        usd_sub = f"Rate ~{rate:.2f}"
        if cny_granted > 0:
            cny_sub = f"Grant ¥{cny_granted:.2f}"
        else:
            cny_sub = f"Top-up ¥{cny_topped_up:.2f}"
    else:
        # Both present natively
        usd_sub = f"Top-up ${usd_topped_up:.2f}" if usd_granted == 0 else f"Grant ${usd_granted:.2f}"
        cny_sub = f"Top-up ¥{cny_topped_up:.2f}" if cny_granted == 0 else f"Grant ¥{cny_granted:.2f}"

    return {
        "ok": True,
        "is_available": is_available,
        "usd_balance": usd_total,
        "usd_text": f"${usd_total:.2f}",
        "usd_sub": usd_sub,
        "cny_balance": cny_total,
        "cny_text": f"¥{cny_total:.2f}",
        "cny_sub": cny_sub,
        "rate": rate,
        "error": "",
    }


def snapshot_from_http(
    status: int,
    body: Any,
    rate: float = DEFAULT_RATE,
) -> dict[str, Any]:
    if status == 401:
        return error_snapshot("Unauthorized -- check DEEPSEEK_API_KEY")
    if status == 429:
        return error_snapshot("Rate limited")
    if status != 200:
        return error_snapshot(f"HTTP error {status}")
    if isinstance(body, (bytes, bytearray)):
        try:
            payload = json.loads(bytes(body).decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return error_snapshot("Malformed JSON body")
    elif isinstance(body, str):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return error_snapshot("Malformed JSON body")
    else:
        payload = body
    return parse_balance(payload, rate=rate)


def build_snapshot(
    env: Optional[Mapping[str, str]] = None,
    creds_path: Optional[os.PathLike[str] | str] = None,
    opener: Optional[Opener] = None,
    rate_opener: Optional[Opener] = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    environ = os.environ if env is None else env
    token = load_token(env=environ, creds_path=creds_path)
    if not token:
        return error_snapshot("No credentials found -- set DEEPSEEK_API_KEY")

    rate = fetch_exchange_rate(opener=rate_opener, timeout=3.0)

    try:
        status, body = fetch_balance(token, opener=opener, timeout=timeout)
    except (URLError, OSError, TimeoutError, ValueError):
        return error_snapshot("Balance request failed")

    return snapshot_from_http(status, body, rate=rate)


def dump_snapshot(snapshot: Mapping[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def write_snapshot(snapshot: Mapping[str, Any], path: os.PathLike[str] | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".tmp")
    staging.write_text(dump_snapshot(snapshot) + "\n", encoding="utf-8")
    os.replace(staging, target)


def read_snapshot(path: os.PathLike[str] | str) -> Optional[dict[str, Any]]:
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def carry_forward(
    fresh: Mapping[str, Any],
    target: os.PathLike[str] | str,
    now_unix: int,
) -> dict[str, Any]:
    if fresh.get("ok"):
        return {**fresh, "fetched_at": now_unix, "checked_at": now_unix, "last_error": ""}

    failure = fresh.get("error") or "Balance fetch failed"
    previous = read_snapshot(target)
    if previous is None or not previous.get("ok"):
        return {
            **fresh,
            "fetched_at": now_unix,
            "checked_at": now_unix,
            "last_error": failure,
        }
    return {**previous, "checked_at": now_unix, "last_error": failure}


def default_snapshot_path() -> Path:
    return Path(__file__).resolve().parent / "snapshot.json"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch DeepSeek API balance")
    parser.add_argument("--out", dest="out", help="Write snapshot JSON to this path")
    parser.add_argument("--creds", dest="creds", help="Override credentials.json path")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = parse_args(argv)
    target = Path(args.out) if args.out else default_snapshot_path()
    now_unix = int(datetime.now(timezone.utc).timestamp())
    snapshot = carry_forward(build_snapshot(creds_path=args.creds), target, now_unix)
    write_snapshot(snapshot, target)
    try:
        sys.stdout.write(dump_snapshot(snapshot) + "\n")
    except Exception:
        try:
            sys.stdout.buffer.write((dump_snapshot(snapshot) + "\n").encode("utf-8", errors="replace"))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
