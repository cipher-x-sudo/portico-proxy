#!/usr/bin/env python3
"""
Print a JSON summary of .ovpn files under an ovpn root, grouped by the first path segment.

  python scripts/scan_ovpn_providers.py
  python scripts/scan_ovpn_providers.py --ovpn-root E:/FB/protonusa/ovpn

Each key under ovpnCountByProvider is the first directory component of each file’s path
relative to the root (e.g. NC, surfshark). Bare files at the root of ovpn-root are counted
under "".
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    default_root = Path(__file__).resolve().parent.parent / "ovpn"
    p.add_argument(
        "--ovpn-root",
        type=Path,
        default=default_root,
        help=f"Directory to scan (default: {default_root})",
    )
    p.add_argument("--pretty", action="store_true", help="Indent JSON output")
    args = p.parse_args()
    root: Path = args.ovpn_root.resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    counts: Counter[str] = Counter()
    for f in root.rglob("*.ovpn"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        key = parts[0] if len(parts) >= 2 else ""
        counts[key] += 1

    providers = sorted(counts.keys(), key=lambda k: (-counts[k], k))
    out = {
        "ovpnRoot": str(root),
        "totalOvpn": sum(counts.values()),
        "ovpnCountByProvider": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "providers": providers,
    }
    indent = 2 if args.pretty else None
    print(json.dumps(out, indent=indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
