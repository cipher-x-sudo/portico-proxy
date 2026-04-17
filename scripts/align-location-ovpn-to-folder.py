#!/usr/bin/env python3
"""
One-off helper: set every locations[].ovpn in openvpn-proxy-config.json to the first
*.ovpn filename found under a given directory (alphabetically). Use when old Proton-style
names no longer exist on disk and you want JSON defaults to match real files.

  python scripts/align-location-ovpn-to-folder.py --config ./backend/openvpn-proxy-config.json --ovpn-dir ./ovpn

Dry-run prints the chosen filename only; add --write to update the config file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--ovpn-dir", type=Path, required=True)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()
    cfg_path: Path = args.config.resolve()
    ovpn_dir: Path = args.ovpn_dir.resolve()
    if not cfg_path.is_file():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        return 1
    if not ovpn_dir.is_dir():
        print(f"OVPN dir not found: {ovpn_dir}", file=sys.stderr)
        return 1
    files = sorted(x.name for x in ovpn_dir.glob("*.ovpn") if x.is_file())
    if not files:
        print(f"No .ovpn files in {ovpn_dir}", file=sys.stderr)
        return 1
    pick = files[0]
    print(f"Using placeholder OVPN filename: {pick} ({len(files)} file(s) in folder)")
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    locs = raw.get("locations") or []
    for i, loc in enumerate(locs):
        if isinstance(loc, dict):
            loc["ovpn"] = pick
    if not args.write:
        print("Dry-run only. Pass --write to save.")
        return 0
    cfg_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {len(locs)} location(s) in {cfg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
