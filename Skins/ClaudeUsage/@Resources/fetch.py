"""Claude Code 5h/7d usage fetch for the Rainmeter ClaudeUsage skin.

Token lookup matches bozdemir/claude-usage-widget:
  1. CLAUDE_CODE_OAUTH_TOKEN
  2. ~/.claude/.credentials.json -> claudeAiOauth.accessToken

Remaining is 100 - utilization, clamped to 0-100. A fetch/parse failure
is an explicit error string and never invents 0% remaining.
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

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "rainmeter-claude-usage"
DEFAULT_CREDS = Path.home() / ".claude" / ".credentials.json"

Opener = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


def clamp_pct(value: Any) -> float:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError("utilization is not a finite number")
    return max(0.0, min(100.0, number))


def remaining_from_utilization(utilization: Any) -> float:
    return 100.0 - clamp_pct(utilization)


def _normalize_iso(value: str) -> str:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return text


def iso_to_unix(resets_at: Any) -> int:
    if not isinstance(resets_at, str) or not resets_at.strip():
        return 0
    try:
        target = datetime.fromisoformat(_normalize_iso(resets_at))
    except (TypeError, ValueError):
        return 0
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return int(target.timestamp())


def format_countdown_seconds(seconds: Any) -> str:
    try:
        remaining = int(seconds)
    except (TypeError, ValueError):
        return "--"
    if remaining <= 0:
        return "now"
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes > 0:
        return f"{minutes}m"
    return "<1m"


def format_countdown(resets_at: Any, now: Optional[datetime] = None) -> str:
    unix = iso_to_unix(resets_at)
    if unix <= 0:
        return "--"
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return format_countdown_seconds(unix - int(clock.timestamp()))


def load_token(
    env: Optional[Mapping[str, str]] = None,
    creds_path: Optional[os.PathLike[str] | str] = None,
) -> Optional[str]:
    environ = os.environ if env is None else env
    env_tok = (environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    path = Path(creds_path) if creds_path is not None else DEFAULT_CREDS
    if not path.is_file():
        return None
    try:
        blob = path.read_text(encoding="utf-8", errors="replace")
        creds = json.loads(blob)
        token = creds["claudeAiOauth"]["accessToken"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    token = (token or "").strip()
    return token or None


def error_snapshot(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def default_opener(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = Request(url, headers=dict(headers), method="GET")
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


def fetch_usage(
    token: str,
    opener: Optional[Opener] = None,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
        "User-Agent": USER_AGENT,
    }
    transport = opener or default_opener
    return transport(USAGE_URL, headers, timeout)


def parse_usage(payload: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return error_snapshot("Unexpected /api/oauth/usage payload")
    five = payload.get("five_hour")
    seven = payload.get("seven_day")
    if not isinstance(five, dict) or not isinstance(seven, dict):
        return error_snapshot("Malformed /api/oauth/usage body")
    if "utilization" not in five or "utilization" not in seven:
        return error_snapshot("Malformed /api/oauth/usage body")
    try:
        session_used = clamp_pct(five["utilization"])
        weekly_used = clamp_pct(seven["utilization"])
    except (TypeError, ValueError):
        return error_snapshot("Malformed /api/oauth/usage body")
    return {
        "ok": True,
        "session_used": session_used,
        "session_remaining": remaining_from_utilization(session_used),
        "weekly_used": weekly_used,
        "weekly_remaining": remaining_from_utilization(weekly_used),
        "session_reset": format_countdown(five.get("resets_at"), now=now),
        "weekly_reset": format_countdown(seven.get("resets_at"), now=now),
        "session_resets_at": five.get("resets_at") or "",
        "weekly_resets_at": seven.get("resets_at") or "",
        "session_reset_unix": iso_to_unix(five.get("resets_at")),
        "weekly_reset_unix": iso_to_unix(seven.get("resets_at")),
        "error": "",
    }


def snapshot_from_http(
    status: int,
    body: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    if status == 401:
        return error_snapshot("Credentials expired -- re-authenticate with 'claude'")
    if status == 429:
        return error_snapshot("Rate limited -- try again later")
    if status != 200:
        return error_snapshot(f"OAuth usage error {status}")
    if isinstance(body, (bytes, bytearray)):
        try:
            payload = json.loads(bytes(body).decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return error_snapshot("Malformed /api/oauth/usage body")
    elif isinstance(body, str):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return error_snapshot("Malformed /api/oauth/usage body")
    else:
        payload = body
    return parse_usage(payload, now=now)


def build_snapshot(
    env: Optional[Mapping[str, str]] = None,
    creds_path: Optional[os.PathLike[str] | str] = None,
    opener: Optional[Opener] = None,
    now: Optional[datetime] = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    token = load_token(env=env, creds_path=creds_path)
    if not token:
        return error_snapshot("No credentials found -- run 'claude' to log in")
    try:
        status, body = fetch_usage(token, opener=opener, timeout=timeout)
    except (URLError, OSError, TimeoutError, ValueError):
        return error_snapshot("OAuth usage request failed")
    return snapshot_from_http(status, body, now=now)


def dump_snapshot(snapshot: Mapping[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def write_snapshot(snapshot: Mapping[str, Any], path: os.PathLike[str] | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_snapshot(snapshot) + "\n", encoding="utf-8")


def default_snapshot_path() -> Path:
    return Path(__file__).resolve().parent / "snapshot.json"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Claude Code 5h/7d remaining limits")
    parser.add_argument("--out", dest="out", help="Write snapshot JSON to this path")
    parser.add_argument("--creds", dest="creds", help="Override credentials.json path")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    snapshot = build_snapshot(creds_path=args.creds)
    target = Path(args.out) if args.out else default_snapshot_path()
    write_snapshot(snapshot, target)
    sys.stdout.write(dump_snapshot(snapshot) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
