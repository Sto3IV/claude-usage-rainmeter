"""Build a validating Rainmeter .rmskin (ZIP + 16-byte RMSKIN footer).

Packages all three skins under the single AIUsageLimits root config, so one
install gives Claude, Grok and Antigravity.
"""

from __future__ import annotations

import argparse
import configparser
import re
import struct
import zipfile
from pathlib import Path

CONFIG_NAME = "AIUsageLimits"

SKIP_NAMES = {"snapshot.json", "python.inc", "Thumbs.db", ".DS_Store"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp"}
SKIP_DIRS = {"__pycache__", "tests"}


def should_skip(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    if any(part in SKIP_DIRS for part in rel_parts):
        return True
    if path.name in SKIP_NAMES:
        return True
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    return False


def read_version(rmskin_ini: Path) -> str:
    """Single source of truth for the version, so the filename cannot drift."""
    parser = configparser.ConfigParser()
    parser.read(rmskin_ini, encoding="utf-8")
    return parser.get("rmskin", "Version", fallback="0.0.0")


def build_rmskin(repo: Path, dest: Path) -> Path:
    skins = repo / "Skins" / CONFIG_NAME
    rmskin_ini = repo / "RMSKIN.ini"
    if not skins.is_dir():
        raise SystemExit(f"missing {skins}")
    if not rmskin_ini.is_file():
        raise SystemExit(f"missing {rmskin_ini}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    packed = 0
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(rmskin_ini, "RMSKIN.ini")
        for path in sorted(skins.rglob("*")):
            if not path.is_file() or should_skip(path, skins):
                continue
            arcname = Path("Skins") / CONFIG_NAME / path.relative_to(skins)
            zf.write(path, arcname.as_posix())
            packed += 1
    if not packed:
        raise SystemExit("packaged nothing -- check the Skins tree")

    zip_size = dest.stat().st_size
    # Rainmeter Skin Installer footer (16 bytes), as used by rmskin-builder:
    # uint32 LE archive size + 5 zero bytes + "RMSKIN\0"
    footer = struct.pack("<I", zip_size) + bytes(5) + b"RMSKIN\x00"
    if len(footer) != 16:
        raise RuntimeError(f"footer must be 16 bytes, got {len(footer)}")
    with dest.open("ab") as fh:
        fh.write(footer)
    print(f"packed {packed} files")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output .rmskin path (default: <repo>/AI_Usage_Limits_<version>.rmskin)",
    )
    args = parser.parse_args()
    version = read_version(args.repo / "RMSKIN.ini")
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", version)
    dest = args.out or (args.repo / f"AI_Usage_Limits_{safe}.rmskin")
    print(build_rmskin(args.repo, dest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
