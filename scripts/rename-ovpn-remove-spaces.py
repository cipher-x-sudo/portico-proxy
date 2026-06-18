#!/usr/bin/env python3
"""
Rename Portico OVPN files by removing whitespace from filenames.

When a compact name already exists (e.g. NewYork alongside New York), the spaced
duplicate is removed instead of renamed.

  python scripts/rename-ovpn-remove-spaces.py --ovpn-root ./ovpn
  python scripts/rename-ovpn-remove-spaces.py --ovpn-root ./ovpn --write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def compact_ovpn_name(name: str) -> str:
    stem = name[:-5] if name.lower().endswith(".ovpn") else name
    return re.sub(r"\s+", "", stem) + ".ovpn"


def plan_renames(ovpn_root: Path) -> tuple[list[tuple[Path, Path]], list[Path]]:
    renames: list[tuple[Path, Path]] = []
    deletions: list[Path] = []
    for path in sorted(ovpn_root.rglob("*.ovpn")):
        if not path.is_file():
            continue
        if not re.search(r"\s", path.name):
            continue
        target_name = compact_ovpn_name(path.name)
        target = path.with_name(target_name)
        if target == path:
            continue
        if target.exists():
            deletions.append(path)
        else:
            renames.append((path, target))
    return renames, deletions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(__file__).resolve().parent.parent / "ovpn"
    parser.add_argument("--ovpn-root", type=Path, default=default_root)
    parser.add_argument("--write", action="store_true", help="Apply changes (default is dry-run)")
    args = parser.parse_args()

    ovpn_root = args.ovpn_root.resolve()
    if not ovpn_root.is_dir():
        print(f"OVPN root not found: {ovpn_root}", file=sys.stderr)
        return 1

    renames, deletions = plan_renames(ovpn_root)
    if not renames and not deletions:
        print(f"No spaced OVPN filenames under {ovpn_root}")
        return 0

    for src, dst in renames:
        rel_src = src.relative_to(ovpn_root).as_posix()
        rel_dst = dst.relative_to(ovpn_root).as_posix()
        print(f"rename: {rel_src} -> {rel_dst}")
    for path in deletions:
        rel = path.relative_to(ovpn_root).as_posix()
        target = path.with_name(compact_ovpn_name(path.name))
        rel_target = target.relative_to(ovpn_root).as_posix()
        print(f"delete duplicate: {rel} (keeping {rel_target})")

    if not args.write:
        print(f"\nDry-run only. Pass --write to apply {len(renames)} rename(s) and {len(deletions)} deletion(s).")
        return 0

    for src, dst in renames:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    for path in deletions:
        path.unlink()

    print(f"\nApplied {len(renames)} rename(s) and {len(deletions)} deletion(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
