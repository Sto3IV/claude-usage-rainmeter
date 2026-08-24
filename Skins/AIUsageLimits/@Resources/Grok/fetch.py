"""Grok SuperGrok weekly usage fetch for the Rainmeter AIUsageLimits/Grok skin.

Token: ~/.grok/auth.json -> first entry .key (OIDC session).
Primary: GET the CLI's own billing route, live on every call.
Fallback: latest `billing: fetched credits config` line in ~/.grok/logs/unified.jsonl.
That line is only rewritten when the CLI itself refetches billing -- at session
start, not on a schedule -- so it can be hours old. It carries its own `ts`, which
becomes fetched_at, so the bar ages it honestly instead of presenting a stale
percent as current.

A log line is never allowed to replace a newer snapshot. HTTP is the only source
that moves with actual usage; one timeout used to rewind the panel to the last
CLI scrape and clear last_error, which also reset the backoff.

Weekly used = creditUsagePercent. Reset = currentPeriod.end / billingPeriodEnd.
A failure is an explicit error, never a fake 0%.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# The /v1 matters. grok.exe carries the path constant "/billing?format=credits" and
# the API base "https://cli-chat-proxy.grok.com/v1"; this list used to join the two
# without the base's suffix, so every live call 404'd and the log fallback silently
# became the only source. Probed 2026-08-23 with no credentials attached, which is
# enough to tell a missing route from a missing token: this URL answers 401 with a
# JSON auth error naming x_xai_token_auth, while grok.com, grok.com/rest, api.x.ai,
# api.x.ai/v1 and this same host without /v1 all answer 404.
API_BASE = "https://cli-chat-proxy.grok.com/v1"
BILLING_PATH = "/billing?format=credits"
BILLING_URLS = (API_BASE + BILLING_PATH,)
DEFAULT_AUTH = Path.home() / ".grok" / "auth.json"
DEFAULT_LOG = Path.home() / ".grok" / "logs" / "unified.jsonl"
USER_AGENT = "rainmeter-grok-usage"
# urlopen measured ~0.2s on a warm process. Rainmeter spawns a cold cmd+Python
# under State=Hide with a 25s kill timer, and three fetchers can start together.
# 3s was enough to lose the race and fall through to the log.
HTTP_TIMEOUT = 8.0
LOG_SCAN_CHUNK = 65536

Opener = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


def clamp_pct(value: Any) -> float:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError("percent is not a finite number")
    return max(0.0, min(100.0, number))


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
    # An unreadable reset time and a window that already lapsed read the same to
    # a user: nothing is counting down. parse.lua renders it identically.
    if remaining <= 0:
        return "--"
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


def error_snapshot(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def load_auth_entry(
    auth_path: Optional[os.PathLike[str] | str] = None,
) -> Optional[dict[str, Any]]:
    path = Path(auth_path) if auth_path is not None else DEFAULT_AUTH
    if not path.is_file():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(blob, dict):
        return None
    for value in blob.values():
        if isinstance(value, dict) and (value.get("key") or "").strip():
            return value
    return None


def load_token(auth_path: Optional[os.PathLike[str] | str] = None) -> Optional[str]:
    env_tok = (os.environ.get("GROK_AUTH") or os.environ.get("XAI_API_KEY") or "").strip()
    if env_tok:
        return env_tok
    entry = load_auth_entry(auth_path)
    if not entry:
        return None
    return (entry.get("key") or "").strip() or None


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


def _auth_headers(token: str, user_id: str = "") -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        # Tracks the installed CLI (the "ver" field on every unified.jsonl line).
        # If the route starts refusing us over a version, this is the knob.
        "x-grok-client-version": "1.0.5",
        "x-grok-client-identifier": "grok-shell",
        "x-grok-client-mode": "cli",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if user_id:
        headers["x-userid"] = user_id
    return headers


def parse_billing(payload: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return error_snapshot("Unexpected Grok billing payload")
    if isinstance(payload.get("config"), dict):
        cfg = payload["config"]
        tier = str(payload.get("subscriptionTier") or cfg.get("subscriptionTier") or "")
    else:
        cfg = payload
        tier = str(payload.get("subscriptionTier") or "")
    if "creditUsagePercent" not in cfg and "currentPeriod" not in cfg and "billingPeriodEnd" not in cfg:
        return error_snapshot("Malformed Grok billing body")
    try:
        used = clamp_pct(cfg.get("creditUsagePercent", 0))
    except (TypeError, ValueError):
        return error_snapshot("Malformed Grok billing body")
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    reset_iso = period.get("end") or cfg.get("billingPeriodEnd") or ""
    if not isinstance(reset_iso, str):
        reset_iso = ""
    return {
        "ok": True,
        "weekly_used": used,
        "weekly_remaining": 100.0 - used,
        "weekly_reset": format_countdown(reset_iso, now=now),
        "weekly_resets_at": reset_iso,
        "weekly_reset_unix": iso_to_unix(reset_iso),
        "subscription_tier": tier,
        "error": "",
    }


def snapshot_from_http(status: int, body: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    if status == 401:
        return error_snapshot("Credentials expired -- run 'grok login'")
    if status == 429:
        return error_snapshot("Rate limited")
    if status != 200:
        return error_snapshot(f"Grok billing error {status}")
    if isinstance(body, (bytes, bytearray)):
        try:
            payload = json.loads(bytes(body).decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return error_snapshot("Malformed Grok billing body")
    elif isinstance(body, str):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return error_snapshot("Malformed Grok billing body")
    else:
        payload = body
    return parse_billing(payload, now=now)


def iter_lines_reversed(path: Path, chunk_size: int = LOG_SCAN_CHUNK) -> Iterator[str]:
    """Yield JSONL lines newest-first without reading the whole file into RAM."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        remaining = handle.tell()
        carry = b""
        while remaining > 0:
            take = min(chunk_size, remaining)
            remaining -= take
            handle.seek(remaining)
            block = handle.read(take) + carry
            parts = block.split(b"\n")
            carry = parts[0]
            for part in reversed(parts[1:]):
                if part:
                    yield part.decode("utf-8", errors="replace")
        if carry:
            yield carry.decode("utf-8", errors="replace")


def read_log_billing(
    log_path: Optional[os.PathLike[str] | str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    path = Path(log_path) if log_path is not None else DEFAULT_LOG
    if not path.is_file():
        return error_snapshot("No credentials found -- run 'grok login'")
    last: Optional[dict[str, Any]] = None
    last_ts = ""
    try:
        for line in iter_lines_reversed(path):
            if "fetched credits config" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ctx = event.get("ctx")
            if isinstance(ctx, dict):
                last = ctx
                ts = event.get("ts")
                last_ts = ts if isinstance(ts, str) else ""
                break
    except OSError:
        return error_snapshot("Could not read Grok billing log")
    if last is None:
        return error_snapshot("No Grok billing data in log -- open grok once")
    snap = parse_billing(last, now=now)
    if snap.get("ok"):
        snap["source"] = "log"
        # When the CLI wrote the line, not when we read it. carry_forward() turns
        # this into fetched_at; without it an hours-old line is stamped `now` and
        # the bar's staleness indicator can never fire.
        snap["data_ts"] = iso_to_unix(last_ts)
    return snap


def fetch_billing(
    token: str,
    user_id: str = "",
    opener: Optional[Opener] = None,
    timeout: float = HTTP_TIMEOUT,
    urls: Optional[tuple[str, ...]] = None,
) -> tuple[int, bytes]:
    transport = opener or default_opener
    headers = _auth_headers(token, user_id)
    last_status, last_body = 0, b""
    for url in urls or BILLING_URLS:
        try:
            status, body = transport(url, headers, timeout)
        except (URLError, OSError, TimeoutError, ValueError):
            continue
        last_status, last_body = status, body
        if status == 200:
            return status, body
        if status in (401, 429):
            return status, body
    if last_status:
        return last_status, last_body
    raise URLError("Grok billing request failed")


def build_snapshot(
    auth_path: Optional[os.PathLike[str] | str] = None,
    log_path: Optional[os.PathLike[str] | str] = None,
    opener: Optional[Opener] = None,
    now: Optional[datetime] = None,
    timeout: float = HTTP_TIMEOUT,
) -> dict[str, Any]:
    # HTTP first: it is the only source that answers with the percent as it stands
    # right now. The log only moves when the CLI itself refetches billing, so
    # preferring it -- as this did while the URL was wrong -- renders an hours-old
    # number with nothing to mark it as old. The log stays as the fallback: a real
    # number carrying an honest age still beats a blank panel. A failed HTTP that
    # then "succeeds" via the log still records last_error, so backoff fires and
    # carry_forward can refuse to rewind a newer snapshot.
    entry = load_auth_entry(auth_path)
    token = load_token(auth_path)
    user_id = str((entry or {}).get("user_id") or "")
    http_error = ""
    if token:
        try:
            status, body = fetch_billing(token, user_id=user_id, opener=opener, timeout=timeout)
            live = snapshot_from_http(status, body, now=now)
            if live.get("ok"):
                live["source"] = "http"
                return live
            http_error = str(live.get("error") or f"Grok billing error {status}")
        except TimeoutError:
            http_error = "Grok billing timed out"
        except (URLError, OSError, ValueError):
            http_error = "Grok billing request failed"
    else:
        http_error = "No credentials found -- run 'grok login'"
    logged = read_log_billing(log_path, now=now)
    if logged.get("ok"):
        if http_error:
            logged["last_error"] = http_error
        return logged
    if not token:
        return error_snapshot("No credentials found -- run 'grok login'")
    if http_error:
        return error_snapshot(http_error)
    return logged if logged.get("error") else error_snapshot("Grok billing request failed")


def dump_snapshot(snapshot: Mapping[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def write_snapshot(snapshot: Mapping[str, Any], path: os.PathLike[str] | str) -> None:
    """Replace the snapshot atomically.

    parse.lua polls this file every few seconds, and skins often live in a
    synced folder, so a plain write can be caught half-flushed or blocked by a
    sync lock. os.replace() is atomic on the same volume: readers see old or
    new, never a half-written file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".tmp")
    staging.write_text(dump_snapshot(snapshot) + "\n", encoding="utf-8")
    os.replace(staging, target)


def read_snapshot(path: os.PathLike[str] | str) -> Optional[dict[str, Any]]:
    """Best-effort read of an existing snapshot. None if absent or unusable."""
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
    """Keep the last good reading when a refresh fails.

    Overwriting good data with an error blanks the whole skin over a hiccup that
    the next cycle would have fixed -- showing a number a few minutes old is far
    better. Only report ok:false when there is nothing to fall back on.

    fetched_at marks when the DATA was obtained; checked_at when we last tried.
    A source that knows when its data was true says so in data_ts -- the log does,
    and stamping `now` over it is exactly what let an hours-old percent pass for
    current. Live HTTP omits it and keeps stamping now, which is correct for it.

    An ok log line older than the snapshot is treated as a failed refresh, not a
    new reading. The CLI log lags live usage; replacing HTTP with it rewinds the
    percent and the stamp together.
    """
    if fresh.get("ok"):
        stamped = dict(fresh)
        try:
            obtained = int(stamped.pop("data_ts", 0) or 0)
        except (TypeError, ValueError):
            obtained = 0
        if obtained <= 0:
            obtained = now_unix
        last_error = str(stamped.get("last_error") or "")
        previous = read_snapshot(target)
        if previous and previous.get("ok"):
            try:
                prev_fetched = int(previous.get("fetched_at") or 0)
            except (TypeError, ValueError):
                prev_fetched = 0
            if prev_fetched > obtained:
                kept = dict(previous)
                kept["checked_at"] = now_unix
                kept["last_error"] = last_error or str(kept.get("last_error") or "stale fallback")
                return kept
        stamped["fetched_at"] = obtained
        stamped["checked_at"] = now_unix
        stamped["last_error"] = last_error
        return stamped

    failure = fresh.get("error") or "Usage fetch failed"
    previous = read_snapshot(target)
    if previous is None or not previous.get("ok"):
        return {
            **fresh,
            "fetched_at": now_unix,
            "checked_at": now_unix,
            "last_error": failure,
        }
    # Deliberately keeps the previous fetched_at: the data did not get any newer.
    return {**previous, "checked_at": now_unix, "last_error": failure}


def default_snapshot_path() -> Path:
    return Path(__file__).resolve().parent / "snapshot.json"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Grok weekly usage")
    parser.add_argument("--out", dest="out", help="Write snapshot JSON to this path")
    parser.add_argument("--auth", dest="auth", help="Override auth.json path")
    parser.add_argument("--log", dest="log", help="Override unified.jsonl path")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    target = Path(args.out) if args.out else default_snapshot_path()
    now_unix = int(datetime.now(timezone.utc).timestamp())
    snapshot = carry_forward(
        build_snapshot(auth_path=args.auth, log_path=args.log), target, now_unix
    )
    write_snapshot(snapshot, target)
    sys.stdout.write(dump_snapshot(snapshot) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
