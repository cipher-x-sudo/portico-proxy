from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class ProviderAuthResult:
    """Resolved OpenVPN credentials for auth-user-pass."""

    provider: str
    auth_path: Path
    username: str
    password: str


def _normalize_ovpn_ref(ovpn_ref: str) -> str:
    s = (ovpn_ref or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _resolve_child_dir_case_insensitive(ovpn_root: Path, name: str) -> Optional[Path]:
    """Return the actual child directory of ovpn_root whose name matches name case-insensitively."""
    if not name or name in (".", ".."):
        return None
    if not ovpn_root.is_dir():
        return None
    want = name.casefold()
    for child in ovpn_root.iterdir():
        if child.is_dir() and child.name.casefold() == want:
            return child
    return None


def _read_auth_txt(auth_path: Path, provider_label: str) -> ProviderAuthResult:
    try:
        lines = auth_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Failed reading auth file: {auth_path} ({exc})") from exc
    non_empty = [line.strip() for line in lines if line.strip()]
    if len(non_empty) < 2:
        raise RuntimeError(
            f"Invalid auth file for {provider_label}: {auth_path}. "
            "Expected username on line 1 and password on line 2."
        )
    return ProviderAuthResult(
        provider=provider_label,
        auth_path=auth_path,
        username=non_empty[0],
        password=non_empty[1],
    )


def _env_credentials() -> Optional[Tuple[str, str]]:
    u = (os.environ.get("OPENVPN_USERNAME") or "").strip()
    p = (os.environ.get("OPENVPN_PASSWORD") or "").strip()
    if u and p:
        return (u, p)
    return None


def load_provider_auth(ovpn_ref: str, ovpn_root: Path) -> ProviderAuthResult:
    """
    Resolve username/password for the selected profile.

    Resolution order:
    1. If ovpn_ref has a parent path (e.g. ``NC/profile.ovpn``), use
       ``<ovpn_root>/<first_segment>/auth.txt`` (provider folder name matched case-insensitively).
    2. Else (bare filename), use ``<ovpn_root>/auth.txt`` if it exists.
    3. Else if ``OPENVPN_USERNAME`` and ``OPENVPN_PASSWORD`` are both set, use those (provider label ``env``).
    """
    root = ovpn_root.resolve()
    norm = _normalize_ovpn_ref(ovpn_ref)
    tried: List[str] = []

    rel = Path(norm)
    parts = rel.parts
    if len(parts) >= 2:
        first = parts[0]
        resolved_dir = _resolve_child_dir_case_insensitive(root, first)
        if resolved_dir is not None:
            candidate = (resolved_dir / "auth.txt").resolve()
            tried.append(str(candidate))
            if candidate.is_file():
                return _read_auth_txt(candidate, resolved_dir.name)
        else:
            tried.append(f"<ovpn_root>/{first}/auth.txt (no matching folder under {root})")

    root_auth = (root / "auth.txt").resolve()
    tried.append(str(root_auth))
    if root_auth.is_file():
        return _read_auth_txt(root_auth, "root")

    env_pair = _env_credentials()
    if env_pair:
        u, p = env_pair
        return ProviderAuthResult(
            provider="env",
            auth_path=root,
            username=u,
            password=p,
        )

    tried.append("OPENVPN_USERNAME + OPENVPN_PASSWORD (both non-empty)")
    raise RuntimeError(
        "Could not resolve OpenVPN credentials for "
        f"{ovpn_ref!r}. Tried: {'; '.join(tried)}"
    )
