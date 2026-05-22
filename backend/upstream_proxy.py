"""
Catalog and parsing helpers for generic upstream proxy profiles.

The gateway stores upstream credentials in a separate catalog so launcher state
can reference stable profile IDs without copying secrets into status payloads.
"""

from __future__ import annotations

import json
import errno
import re
import secrets
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


CATALOG_VERSION = 1
SUPPORTED_UPSTREAM_SCHEMES = {"http", "socks5"}
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class UpstreamProxyError(ValueError):
    """Raised when an upstream proxy profile or import line is invalid."""


def resolve_catalog_path(config_path: Path, override: str = "") -> Path:
    explicit = override.strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (config_path.parent / "upstream-proxy-catalog.json").resolve()


def _clean_text(value: Any, field: str, max_len: int, allow_empty: bool = True) -> str:
    text = str(value if value is not None else "").strip()
    if not allow_empty and not text:
        raise UpstreamProxyError(f"{field} is required")
    if len(text) > max_len:
        raise UpstreamProxyError(f"{field} must be {max_len} characters or fewer")
    if any(c in text for c in "\r\n\t\x00"):
        raise UpstreamProxyError(f"{field} contains invalid characters")
    return text


def _new_profile_id() -> str:
    return "proxy-" + secrets.token_hex(12)


def normalize_profile(
    raw: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
    allow_new_id: bool = True,
) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise UpstreamProxyError("proxy profile must be an object")

    existing = existing or {}
    raw_id = raw.get("id", existing.get("id", ""))
    profile_id = _clean_text(raw_id, "id", 128)
    if not profile_id:
        if not allow_new_id:
            raise UpstreamProxyError("id is required")
        profile_id = _new_profile_id()
    if not _PROFILE_ID_RE.fullmatch(profile_id):
        raise UpstreamProxyError("id must contain only letters, numbers, dot, underscore, or dash")

    scheme = _clean_text(raw.get("scheme", existing.get("scheme", "http")), "scheme", 16, False).lower()
    if scheme not in SUPPORTED_UPSTREAM_SCHEMES:
        raise UpstreamProxyError('scheme must be "http" or "socks5"')

    host = _clean_text(raw.get("host", existing.get("host", "")), "host", 255, False)
    if any(c in host for c in "/?#@") or any(c.isspace() for c in host):
        raise UpstreamProxyError("host must be a hostname or IP address without URL punctuation")

    raw_port = raw.get("port", existing.get("port"))
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise UpstreamProxyError("port must be an integer") from None
    if port < 1 or port > 65535:
        raise UpstreamProxyError("port must be between 1 and 65535")

    username = _clean_text(raw.get("username", existing.get("username", "")), "username", 1024)
    if "password" in raw:
        password = _clean_text(raw.get("password"), "password", 2048)
    else:
        password = _clean_text(existing.get("password", ""), "password", 2048)
    label_default = f"{host}:{port}"
    label = _clean_text(raw.get("label", existing.get("label", label_default)), "label", 256)

    return {
        "id": profile_id,
        "label": label or label_default,
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


def public_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": profile["id"],
        "label": profile.get("label") or f"{profile.get('host', '')}:{profile.get('port', '')}",
        "scheme": profile.get("scheme") or "http",
        "host": profile.get("host") or "",
        "port": profile.get("port"),
        "username": profile.get("username") or "",
        "hasPassword": bool(profile.get("password")),
    }


def catalog_payload(profiles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "version": CATALOG_VERSION,
        "proxies": list(profiles),
    }


def load_catalog(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    if not path.is_file():
        raise UpstreamProxyError(f"upstream proxy catalog path is not a file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise UpstreamProxyError(f"could not read upstream proxy catalog: {e}") from e
    rows = raw.get("proxies") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise UpstreamProxyError("upstream proxy catalog must contain a proxies array")

    profiles: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        profile = normalize_profile(row, allow_new_id=False)
        if profile["id"] in seen:
            raise UpstreamProxyError(f"duplicate upstream proxy id: {profile['id']}")
        seen.add(profile["id"])
        profiles.append(profile)
    return profiles


def save_catalog(path: Path, profiles: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise UpstreamProxyError(
            f"upstream proxy catalog path is a directory, not a file: {path}. "
            "Create the host JSON file before starting Docker, then recreate the gateway container."
        )
    tmp = path.parent / (path.name + ".tmp")
    if tmp.exists() and not tmp.is_file():
        raise UpstreamProxyError(f"upstream proxy catalog temp path is a directory, not a file: {tmp}")
    text = json.dumps(catalog_payload(profiles), indent=2) + "\n"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        tmp.replace(path)
    except OSError as e:
        # Docker bind-mounted single files can reject atomic replacement with EBUSY
        # because the target path is a mount point. Fall back to updating the file
        # contents in place while preserving the same mounted inode.
        if e.errno not in (errno.EBUSY, errno.EXDEV):
            raise
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            tmp.unlink()
        except OSError:
            pass


def _decoded_url_value(value: Optional[str]) -> str:
    return urllib.parse.unquote(value) if value else ""


def parse_proxy_line(line: str) -> Dict[str, Any]:
    text = line.strip()
    if not text:
        raise UpstreamProxyError("proxy line is empty")

    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        scheme = (parsed.scheme or "").lower()
        if scheme not in SUPPORTED_UPSTREAM_SCHEMES:
            raise UpstreamProxyError('URL scheme must be "http" or "socks5"')
        if not parsed.hostname:
            raise UpstreamProxyError("URL must include a host")
        try:
            port = parsed.port
        except ValueError:
            raise UpstreamProxyError("URL port is invalid") from None
        if port is None:
            raise UpstreamProxyError("URL must include a port")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise UpstreamProxyError("proxy URL must not include path, query, or fragment")
        return normalize_profile(
            {
                "scheme": scheme,
                "host": parsed.hostname,
                "port": port,
                "username": _decoded_url_value(parsed.username),
                "password": _decoded_url_value(parsed.password),
            }
        )

    parts = text.split(":", 3)
    if len(parts) not in (2, 4):
        raise UpstreamProxyError("line must be host:port or host:port:user:pass")
    raw: Dict[str, Any] = {
        "scheme": "http",
        "host": parts[0],
        "port": parts[1],
    }
    if len(parts) == 4:
        raw["username"] = parts[2]
        raw["password"] = parts[3]
    return normalize_profile(raw)


def import_proxy_lines(lines: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    profiles: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    for index, line in enumerate(lines.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            profile = parse_proxy_line(line)
            profiles.append(profile)
            results.append({"line": index, "ok": True, "proxy": public_profile(profile)})
        except UpstreamProxyError as e:
            results.append({"line": index, "ok": False, "error": str(e)})
    return profiles, results


def profile_remote_uri(profile: Dict[str, Any]) -> str:
    """Build a pproxy remote URI without leaking credentials to other payloads."""
    host = profile["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    uri = f"{profile['scheme']}://{host}:{int(profile['port'])}"
    username = profile.get("username") or ""
    password = profile.get("password") or ""
    if username or password:
        auth = urllib.parse.quote(username, safe="") + ":" + urllib.parse.quote(password, safe="")
        uri += "#" + auth
    return uri
