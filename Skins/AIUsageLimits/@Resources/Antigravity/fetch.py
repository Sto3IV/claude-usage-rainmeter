"""Antigravity 5h/7d usage fetch for the Rainmeter AIUsageLimits/Antigravity skin.

Primary: local language_server RetrieveUserQuotaSummary (Antigravity app, then IDE).
Used percent is 100 - remainingFraction*100, clamped 0-100.
The skin follows Claude: Session (5h) used, then Weekly (7d) used, Gemini group.
A failure is an explicit error, never a fake 0%.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "rainmeter-antigravity-usage"
CONNECT_PATH = "/exa.language_server_pb.LanguageServerService"
SUMMARY_RPC = "RetrieveUserQuotaSummary"
STATUS_RPC = "GetUserStatus"
PROCESS_NAMES = ("language_server.exe", "language_server_windows_x64.exe")

Opener = Callable[[str, Mapping[str, str], float, Optional[bytes]], tuple[int, bytes]]


def clamp_pct(value: Any) -> float:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError("percent is not a finite number")
    return max(0.0, min(100.0, number))


def remaining_from_utilization(utilization: Any) -> float:
    return 100.0 - clamp_pct(utilization)


def used_from_remaining_fraction(fraction: Any) -> float:
    return clamp_pct((1.0 - float(fraction)) * 100.0)


def _normalize_iso(value: str) -> str:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return text


def iso_to_unix(resets_at: Any) -> int:
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
        number = float(resets_at)
        if number > 1e12:
            number /= 1000.0
        if math.isnan(number) or math.isinf(number) or number <= 0:
            return 0
        return int(number)
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

    The language server is only reachable while Antigravity is running, so
    failures are routine. Overwriting good data with an error blanks the whole
    skin -- showing the last known quota is far more useful. Only report
    ok:false when there is nothing to fall back on.

    fetched_at marks when the DATA was obtained; checked_at when we last tried.
    """
    if fresh.get("ok"):
        return {**fresh, "fetched_at": now_unix, "checked_at": now_unix, "last_error": ""}

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


def _classify_kind(command_line: str) -> str:
    low = command_line.lower()
    if "antigravity-ide" in low or "subclient_type ide" in low.replace("=", " "):
        return "ide"
    if "--standalone" in low or "--app_data_dir antigravity" in low or "\\antigravity\\resources\\bin\\language_server" in low:
        return "app"
    return "unknown"


def _extract_csrf(command_line: str) -> str:
    match = re.search(r"--csrf_token(?:\s+|=)(\S+)", command_line, flags=re.IGNORECASE)
    return (match.group(1) if match else "").strip()


def list_language_server_processes() -> list[dict[str, Any]]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'language_server*' } | "
        "ForEach-Object { '{0}\t{1}' -f $_.ProcessId, $_.CommandLine }"
    )
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if "\t" not in line:
            continue
        pid_text, command = line.split("\t", 1)
        try:
            pid = int(pid_text.strip())
        except ValueError:
            continue
        command = command.strip()
        if not command:
            continue
        found.append(
            {
                "pid": pid,
                "command": command,
                "kind": _classify_kind(command),
                "csrf": _extract_csrf(command),
            }
        )
    return found


def list_listening_ports(pid: int) -> list[int]:
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    ports: list[int] = []
    needle = re.compile(
        rf"^\s*TCP\s+127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+{pid}\s*$",
        flags=re.IGNORECASE,
    )
    for line in completed.stdout.splitlines():
        match = needle.match(line)
        if not match:
            continue
        port = int(match.group(1))
        if port not in ports:
            ports.append(port)
    return ports


def loopback_opener(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    body: Optional[bytes] = None,
) -> tuple[int, bytes]:
    request = Request(url, data=body if body is not None else b"{}", method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    context = None
    if url.startswith("https://127.0.0.1:") or url.startswith("https://localhost:"):
        context = ssl._create_unverified_context()
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            return int(response.getcode()), response.read(262144)
    except HTTPError as exc:
        payload = b""
        try:
            payload = exc.read(65536)
        except OSError:
            payload = b""
        return int(exc.code), payload


def _rpc_headers(csrf: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if csrf:
        headers["X-Codeium-Csrf-Token"] = csrf
    return headers


def _decode_json(body: Any) -> Any:
    if isinstance(body, (bytes, bytearray)):
        return json.loads(bytes(body).decode("utf-8", errors="replace"))
    if isinstance(body, str):
        return json.loads(body)
    return body


def _bucket_remaining_fraction(bucket: Mapping[str, Any]) -> Optional[float]:
    if "remainingFraction" in bucket:
        try:
            return float(bucket["remainingFraction"])
        except (TypeError, ValueError):
            return None
    remaining = bucket.get("remaining")
    if isinstance(remaining, Mapping) and "remainingFraction" in remaining:
        try:
            return float(remaining["remainingFraction"])
        except (TypeError, ValueError):
            return None
    return None


def _window_kind(bucket: Mapping[str, Any]) -> str:
    window = str(bucket.get("window") or "").lower()
    ident = f"{bucket.get('bucketId') or ''} {bucket.get('displayName') or ''}".lower()
    if window in {"weekly", "7d", "seven_day"} or "weekly" in ident or "7d" in ident:
        return "weekly"
    if window in {"5h", "five_hour", "session"} or "5h" in ident or "five hour" in ident or "session" in ident:
        return "session"
    return ""


def _better_window(current: Optional[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any]:
    if current is None:
        return candidate
    if candidate["used"] > current["used"] + 0.05:
        return candidate
    if abs(candidate["used"] - current["used"]) <= 0.05:
        left = current.get("reset_unix") or 0
        right = candidate.get("reset_unix") or 0
        if right and (not left or right < left):
            return candidate
    return current


def _is_gemini_group(group: Mapping[str, Any]) -> bool:
    name = str(group.get("displayName") or "").lower()
    if "gemini" in name:
        return True
    buckets = group.get("buckets")
    if not isinstance(buckets, list):
        return False
    return any(
        str(bucket.get("bucketId") or "").lower().startswith("gemini")
        for bucket in buckets
        if isinstance(bucket, dict)
    )


def parse_quota_summary(payload: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return error_snapshot("Unexpected Antigravity quota payload")
    root = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    groups = root.get("groups")
    if not isinstance(groups, list) or not groups:
        return error_snapshot("Malformed Antigravity quota body")

    typed = [group for group in groups if isinstance(group, dict)]
    selected = [group for group in typed if _is_gemini_group(group)] or typed

    session: Optional[dict[str, Any]] = None
    weekly: Optional[dict[str, Any]] = None
    parsed_groups = 0
    for group in selected:
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        parsed_groups += 1
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            fraction = _bucket_remaining_fraction(bucket)
            if fraction is None:
                continue
            try:
                used = used_from_remaining_fraction(fraction)
            except (TypeError, ValueError):
                continue
            kind = _window_kind(bucket)
            if not kind:
                continue
            reset_iso = bucket.get("resetTime") or ""
            if not isinstance(reset_iso, str):
                reset_iso = str(reset_iso) if reset_iso else ""
            row = {
                "used": used,
                "remaining": remaining_from_utilization(used),
                "resets_at": reset_iso,
                "reset_unix": iso_to_unix(reset_iso),
                "group": str(group.get("displayName") or ""),
                "bucket": str(bucket.get("bucketId") or bucket.get("displayName") or ""),
            }
            if kind == "session":
                session = _better_window(session, row)
            elif kind == "weekly":
                weekly = _better_window(weekly, row)

    if parsed_groups <= 0 or (session is None and weekly is None):
        return error_snapshot("Malformed Antigravity quota body")

    snapshot: dict[str, Any] = {
        "ok": True,
        "session_used": session["used"] if session else 0.0,
        "session_remaining": session["remaining"] if session else 0.0,
        "weekly_used": weekly["used"] if weekly else 0.0,
        "weekly_remaining": weekly["remaining"] if weekly else 0.0,
        "session_reset": format_countdown(session["resets_at"], now=now) if session else "--",
        "weekly_reset": format_countdown(weekly["resets_at"], now=now) if weekly else "--",
        "session_resets_at": session["resets_at"] if session else "",
        "weekly_resets_at": weekly["resets_at"] if weekly else "",
        "session_reset_unix": session["reset_unix"] if session else 0,
        "weekly_reset_unix": weekly["reset_unix"] if weekly else 0,
        "error": "",
    }
    if session is None:
        snapshot["session_used"] = None
        snapshot["session_remaining"] = None
        snapshot["session_used_text"] = "--"
    if weekly is None:
        snapshot["weekly_used"] = None
        snapshot["weekly_remaining"] = None
        snapshot["weekly_used_text"] = "--"
    return snapshot


def parse_user_status(payload: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return error_snapshot("Unexpected Antigravity status payload")
    status = payload.get("userStatus") if isinstance(payload.get("userStatus"), dict) else payload
    cascade = status.get("cascadeModelConfigData")
    configs = cascade.get("clientModelConfigs") if isinstance(cascade, dict) else None
    if not isinstance(configs, list):
        return error_snapshot("Malformed Antigravity status body")

    session: Optional[dict[str, Any]] = None
    for item in configs:
        if not isinstance(item, dict):
            continue
        quota = item.get("quotaInfo")
        if not isinstance(quota, dict):
            continue
        fraction = _bucket_remaining_fraction(quota)
        if fraction is None:
            continue
        try:
            used = used_from_remaining_fraction(fraction)
        except (TypeError, ValueError):
            continue
        reset_iso = quota.get("resetTime") or ""
        if not isinstance(reset_iso, str):
            reset_iso = ""
        row = {
            "used": used,
            "remaining": remaining_from_utilization(used),
            "resets_at": reset_iso,
            "reset_unix": iso_to_unix(reset_iso),
            "group": str(item.get("label") or item.get("modelId") or ""),
            "bucket": "status-5h",
        }
        session = _better_window(session, row)
    if session is None:
        return error_snapshot("Malformed Antigravity status body")
    return {
        "ok": True,
        "session_used": session["used"],
        "session_remaining": session["remaining"],
        "weekly_used": None,
        "weekly_remaining": None,
        "session_reset": format_countdown(session["resets_at"], now=now),
        "weekly_reset": "--",
        "session_resets_at": session["resets_at"],
        "weekly_resets_at": "",
        "session_reset_unix": session["reset_unix"],
        "weekly_reset_unix": 0,
        "error": "",
    }


def snapshot_from_http(status: int, body: Any, now: Optional[datetime] = None, rpc: str = SUMMARY_RPC) -> dict[str, Any]:
    if status == 401:
        return error_snapshot("Credentials expired -- sign in to Antigravity")
    if status == 429:
        return error_snapshot("Rate limited")
    if status == 404:
        return error_snapshot("Antigravity quota endpoint missing")
    if status != 200:
        return error_snapshot(f"Antigravity quota error {status}")
    try:
        payload = _decode_json(body)
    except json.JSONDecodeError:
        return error_snapshot("Malformed Antigravity quota body")
    if rpc == STATUS_RPC:
        return parse_user_status(payload, now=now)
    return parse_quota_summary(payload, now=now)


def fetch_rpc(
    host: str,
    port: int,
    rpc: str,
    csrf: str,
    opener: Optional[Opener] = None,
    timeout: float = 3.0,
    schemes: Iterable[str] = ("https", "http"),
) -> tuple[int, bytes]:
    transport = opener or loopback_opener
    headers = _rpc_headers(csrf)
    last_status, last_body = 0, b""
    for scheme in schemes:
        url = f"{scheme}://{host}:{port}{CONNECT_PATH}/{rpc}"
        try:
            status, body = transport(url, headers, timeout, b"{}")
        except (URLError, OSError, TimeoutError, ValueError, ssl.SSLError):
            continue
        last_status, last_body = status, body
        if status == 200:
            return status, body
        if status in (401, 429):
            return status, body
    if last_status:
        return last_status, last_body
    raise URLError("Antigravity language server request failed")


def _probe_server(
    server: Mapping[str, Any],
    opener: Optional[Opener],
    now: Optional[datetime],
    timeout: float,
) -> dict[str, Any]:
    csrf = str(server.get("csrf") or "")
    ports = list(server.get("ports") or [])
    last_error = "Antigravity language server request failed"
    for port in ports:
        for rpc in (SUMMARY_RPC, STATUS_RPC):
            try:
                status, body = fetch_rpc(
                    "127.0.0.1",
                    int(port),
                    rpc,
                    csrf,
                    opener=opener,
                    timeout=timeout,
                )
            except (URLError, OSError, TimeoutError, ValueError, ssl.SSLError):
                continue
            snap = snapshot_from_http(status, body, now=now, rpc=rpc)
            if snap.get("ok"):
                snap["source"] = f"local-{server.get('kind') or 'unknown'}"
                return snap
            last_error = str(snap.get("error") or last_error)
    return error_snapshot(last_error)


def build_snapshot(
    opener: Optional[Opener] = None,
    now: Optional[datetime] = None,
    timeout: float = 3.0,
    servers: Optional[list[Mapping[str, Any]]] = None,
    process_lister: Optional[Callable[[], list[dict[str, Any]]]] = None,
    port_lister: Optional[Callable[[int], list[int]]] = None,
) -> dict[str, Any]:
    discovered: list[Mapping[str, Any]]
    if servers is not None:
        discovered = list(servers)
    else:
        lister = process_lister or list_language_server_processes
        ports_of = port_lister or list_listening_ports
        discovered = []
        for process in lister():
            item = dict(process)
            item["ports"] = ports_of(int(item["pid"]))
            discovered.append(item)

    if not discovered:
        return error_snapshot("Open Antigravity -- language server not found")

    ranked = sorted(
        discovered,
        key=lambda item: {"app": 0, "ide": 1, "unknown": 2}.get(str(item.get("kind") or ""), 3),
    )
    last = error_snapshot("Antigravity quota request failed")
    for server in ranked:
        if not server.get("ports"):
            last = error_snapshot("Open Antigravity -- language server has no port")
            continue
        snap = _probe_server(server, opener=opener, now=now, timeout=timeout)
        if snap.get("ok"):
            return snap
        last = snap
    return last


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Antigravity 5h/7d used limits")
    parser.add_argument("--out", dest="out", help="Write snapshot JSON to this path")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    target = Path(args.out) if args.out else default_snapshot_path()
    now_unix = int(datetime.now(timezone.utc).timestamp())
    snapshot = carry_forward(build_snapshot(), target, now_unix)
    write_snapshot(snapshot, target)
    sys.stdout.write(dump_snapshot(snapshot) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
