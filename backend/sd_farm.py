from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_DB_RELATIVE_PATH = Path("DB") / "data" / "accounts.sqlite"
IMPORTED_DB_PATH = Path("/data/sd-farm/accounts.sqlite")
SD_FARM_IMPORT_MAX_BYTES = 64 * 1024 * 1024
ACCOUNT_COLUMNS = ("UID", "Name", "OpenVPN", "Proxy", "Status", "Current_Status")


class SDFarmError(RuntimeError):
    pass


class IXBrowserError(RuntimeError):
    pass


IXBROWSER_DEFAULT_PORT = 53200
IXBROWSER_API_SUFFIX = "/api/v2/"


def _looks_like_ipv4(value: str) -> bool:
    parts = str(value or "").strip().split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False


def _is_loopback_ip(value: str) -> bool:
    return str(value or "").strip().startswith("127.")


def _discover_default_gateway_ip() -> Optional[str]:
    import subprocess

    try:
        proc = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if "via" not in parts:
            continue
        idx = parts.index("via")
        if idx + 1 >= len(parts):
            continue
        ip = parts[idx + 1].strip()
        if _looks_like_ipv4(ip) and not _is_loopback_ip(ip):
            return ip
    return None


def _windows_host_ip_from_env() -> Optional[str]:
    for key in ("IXBROWSER_WINDOWS_HOST", "WSL_WINDOWS_HOST_IP"):
        ip = str(os.environ.get(key) or "").strip()
        if _looks_like_ipv4(ip) and not _is_loopback_ip(ip):
            return ip
    return None


IXBROWSER_DEFAULT_PORT = 53200
IXBROWSER_API_SUFFIX = "/api/v2/"
DOCKER_HOST_NETWORK_PROBE_IMAGE_ENV = "DOCKER_HOST_NETWORK_PROBE_IMAGE"
DEFAULT_DOCKER_HOST_NETWORK_PROBE_IMAGE = "alpine:3.20"

_windows_host_ip_cache: Optional[str] = None
_windows_host_ip_cache_attempted = False


def _running_inside_docker() -> bool:
    return Path("/.dockerenv").is_file()


def _docker_socket_available() -> bool:
    return Path("/var/run/docker.sock").exists()


def _docker_client():
    try:
        import docker
    except ImportError:
        return None
    try:
        return docker.from_env()
    except Exception:
        return None


def _should_ixbrowser_use_host_network() -> bool:
    mode = str(os.environ.get("IXBROWSER_USE_HOST_NETWORK") or "auto").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return False
    if mode in ("1", "true", "yes", "on"):
        return _running_inside_docker() and _docker_socket_available()
    return _running_inside_docker() and _docker_socket_available()


def _ixbrowser_url_for_host_network(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    port = parsed.port or IXBROWSER_DEFAULT_PORT
    win_ip = discover_wsl_windows_host_ip()
    if not win_ip:
        return url
    host = (parsed.hostname or "").lower()
    rewrite_hosts = {
        "host.docker.internal",
        "172.17.0.1",
        "127.0.0.11",
        "127.0.0.1",
        "localhost",
    }
    if host in rewrite_hosts or host.startswith("172.17."):
        return urllib.parse.urlunparse(parsed._replace(netloc=f"{win_ip}:{port}"))
    return url


def _ixbrowser_request_url(url: str) -> str:
    if not _running_inside_docker():
        return url
    return _ixbrowser_url_for_host_network(url)


def _http_post_via_docker_host_network(url: str, payload: Dict[str, Any], timeout: float) -> str:
    client = _docker_client()
    if client is None:
        raise IXBrowserError("Docker client is not available for host-network ixBrowser access")
    target_url = _ixbrowser_url_for_host_network(url)
    body_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    image = (
        str(os.environ.get(DOCKER_HOST_NETWORK_PROBE_IMAGE_ENV) or "").strip()
        or DEFAULT_DOCKER_HOST_NETWORK_PROBE_IMAGE
    )
    timeout_seconds = max(1, int(timeout))
    shell = (
        "BODY=$(printf %s \"$IXBODY_B64\" | base64 -d) && "
        f"wget -qO- --timeout={timeout_seconds} "
        "--header='Content-Type: application/json' "
        "--post-data=\"$BODY\" \"$IXURL\""
    )
    try:
        output = client.containers.run(
            image,
            command=["sh", "-c", shell],
            environment={"IXBODY_B64": body_b64, "IXURL": target_url},
            network_mode="host",
            remove=True,
            stdout=True,
            stderr=True,
        )
    except Exception as e:
        detail = str(e).strip() or repr(e)
        raise IXBrowserError(
            f"ixBrowser host-network request failed ({target_url}): {detail}"
        ) from e
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output or "")


def _discover_windows_host_via_docker_host_network() -> Optional[str]:
    """
    On WSL Docker, the gateway container only sees the Docker bridge (172.17.0.1).
    Spawn a short-lived host-network container to read the WSL default route gateway,
    which is the Windows host IP where ixBrowser listens.
    """
    if not _running_inside_docker() or not _docker_socket_available():
        return None
    client = _docker_client()
    if client is None:
        return None
    image = (
        str(os.environ.get(DOCKER_HOST_NETWORK_PROBE_IMAGE_ENV) or "").strip()
        or DEFAULT_DOCKER_HOST_NETWORK_PROBE_IMAGE
    )
    try:
        output = client.containers.run(
            image,
            command=["sh", "-c", "ip -4 route show default | awk '{print $3; exit}'"],
            network_mode="host",
            remove=True,
            stdout=True,
            stderr=False,
        )
    except Exception:
        return None
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output or "")
    for line in text.splitlines():
        ip = line.strip()
        if _looks_like_ipv4(ip) and not _is_loopback_ip(ip):
            return ip
    return None


def discover_wsl_windows_host_ip(*, force_refresh: bool = False) -> Optional[str]:
    global _windows_host_ip_cache, _windows_host_ip_cache_attempted

    env_ip = _windows_host_ip_from_env()
    if env_ip:
        return env_ip

    if not force_refresh and _windows_host_ip_cache_attempted:
        return _windows_host_ip_cache

    _windows_host_ip_cache_attempted = True
    resolved: Optional[str] = None

    resolved = _discover_windows_host_via_docker_host_network()
    if resolved:
        _windows_host_ip_cache = resolved
        return resolved

    resolv = Path("/etc/resolv.conf")
    if resolv.is_file():
        try:
            for line in resolv.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("nameserver"):
                    parts = stripped.split()
                    if len(parts) >= 2:
                        ip = parts[1].strip()
                        if _looks_like_ipv4(ip) and not _is_loopback_ip(ip):
                            resolved = ip
                            break
        except OSError:
            pass

    if not resolved:
        resolved = _discover_default_gateway_ip()

    _windows_host_ip_cache = resolved
    return resolved


def normalize_ixbrowser_api_base(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if not value.endswith("/"):
        value += "/"
    return value


def build_ixbrowser_api_base(host: str, port: int = IXBROWSER_DEFAULT_PORT) -> str:
    host_text = str(host or "").strip().rstrip("/")
    if host_text.startswith("http://") or host_text.startswith("https://"):
        return normalize_ixbrowser_api_base(host_text)
    return normalize_ixbrowser_api_base(f"http://{host_text}:{int(port)}{IXBROWSER_API_SUFFIX}")


def ixbrowser_api_candidates(
    *,
    use_docker: bool,
    configured_base: str = "",
    override_base: Optional[str] = None,
) -> List[str]:
    seen: set[str] = set()
    candidates: List[str] = []

    def add(raw: str) -> None:
        base = normalize_ixbrowser_api_base(raw)
        if not base or base in seen:
            return
        seen.add(base)
        candidates.append(base)

    if override_base:
        add(override_base)
    configured = normalize_ixbrowser_api_base(configured_base)
    if configured:
        add(configured)
    if use_docker:
        wsl_ip = discover_wsl_windows_host_ip()
        if wsl_ip:
            add(build_ixbrowser_api_base(wsl_ip))
        add(build_ixbrowser_api_base("host.docker.internal"))
        add(build_ixbrowser_api_base("172.17.0.1"))
    else:
        add(build_ixbrowser_api_base("127.0.0.1"))
    return candidates


def ixbrowser_probe_hint(use_docker: bool, tried_urls: List[str]) -> str:
    del tried_urls
    wsl_ip = discover_wsl_windows_host_ip()
    if use_docker and wsl_ip:
        return (
            "ixBrowser usually runs on Windows when using WSL Docker. "
            f"Try http://{wsl_ip}:53200/api/v2/ or ensure ixBrowser is running and port 53200 is allowed through Windows Firewall."
        )
    if use_docker:
        return (
            "ixBrowser usually runs on Windows when using WSL Docker. "
            "Ensure ixBrowser is running and port 53200 is allowed through Windows Firewall. "
            "You can set IXBROWSER_WINDOWS_HOST in .env as a fallback."
        )
    return "Ensure ixBrowser is running and listening on port 53200."


def probe_ixbrowser_bases(
    candidates: Iterable[str],
    *,
    use_docker: bool = False,
    timeout: float = 4.0,
) -> Dict[str, Any]:
    tried: List[str] = []
    last_error = ""
    candidate_list = [normalize_ixbrowser_api_base(base) for base in candidates if normalize_ixbrowser_api_base(base)]
    wsl_ip = discover_wsl_windows_host_ip()
    recommended_base = build_ixbrowser_api_base(wsl_ip) if wsl_ip and use_docker else ""

    for base in candidate_list:
        tried.append(base)
        try:
            profiles = fetch_ixbrowser_profiles(base, page_limit=1, timeout=timeout)
            working_base = build_ixbrowser_api_base(wsl_ip) if wsl_ip and use_docker else base
            return {
                "ok": True,
                "ixBrowserApiBase": working_base,
                "ixBrowserError": "",
                "ixBrowserProfileCount": len(profiles),
                "triedUrls": tried,
                "recommendedBase": working_base,
                "hint": "",
                "wslHostIp": wsl_ip or "",
            }
        except IXBrowserError as e:
            last_error = str(e)

    hint = ixbrowser_probe_hint(use_docker, tried)
    error_text = last_error or "Could not connect to ixBrowser"
    if hint and hint not in error_text:
        error_text = f"{error_text}. {hint}"
    return {
        "ok": False,
        "ixBrowserApiBase": candidate_list[0] if candidate_list else "",
        "ixBrowserError": error_text,
        "ixBrowserProfileCount": 0,
        "triedUrls": tried,
        "recommendedBase": recommended_base,
        "hint": hint,
        "wslHostIp": wsl_ip or "",
    }


def resolve_sd_farm_root(raw: str) -> Path:
    value = (raw or "").strip() or "/sd-farm"
    return Path(value).expanduser()


def sd_farm_source_value(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return "import" if value == "import" else "server"


def save_imported_accounts_db(data: bytes, dest: Path = IMPORTED_DB_PATH) -> int:
    if not data:
        raise SDFarmError("Empty database file")
    if len(data) > SD_FARM_IMPORT_MAX_BYTES:
        raise SDFarmError(f"Database file is too large (max {SD_FARM_IMPORT_MAX_BYTES // (1024 * 1024)} MB)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp")
    try:
        tmp.write_bytes(data)
        load_accounts(tmp, limit=1)
        account_count = len(load_accounts(tmp))
    except SDFarmError:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise SDFarmError(f"Could not save imported database: {e}") from e
    try:
        tmp.replace(dest)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise SDFarmError(f"Could not save imported database: {e}") from e
    return account_count


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def sd_farm_browse_roots(current_root: Optional[Path] = None) -> List[Path]:
    roots: List[Path] = []
    seen: set[str] = set()
    for raw in (
        current_root,
        Path("/sd-farm"),
        Path.home(),
        Path("/"),
    ):
        if raw is None:
            continue
        try:
            candidate = raw.expanduser().resolve()
        except OSError:
            continue
        if not candidate.is_dir():
            continue
        key = _path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        roots.append(candidate)
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:/")
            if not drive.is_dir():
                continue
            key = _path_key(drive)
            if key in seen:
                continue
            seen.add(key)
            roots.append(drive)
    return roots


def _accounts_db_hint(path: Path) -> Tuple[bool, str]:
    try:
        found, _candidates = discover_accounts_db(path)
        return True, str(found)
    except SDFarmError:
        return False, ""


def browse_directory(path: Path, *, limit: int = 200) -> Dict[str, Any]:
    try:
        resolved = path.expanduser().resolve()
    except OSError as e:
        raise SDFarmError(f"Invalid path: {e}") from e
    if not resolved.is_dir():
        raise SDFarmError(f"Not a directory: {resolved}")
    parent = resolved.parent
    parent_path = "" if parent == resolved else parent.as_posix()
    entries: List[Dict[str, Any]] = []
    truncated = False
    try:
        children = sorted(
            [child for child in resolved.iterdir() if child.is_dir() and not child.name.startswith(".")],
            key=lambda child: child.name.lower(),
        )
    except OSError as e:
        raise SDFarmError(f"Could not read directory: {e}") from e
    if len(children) > limit:
        truncated = True
        children = children[:limit]
    for child in children:
        has_db, db_path = _accounts_db_hint(child)
        entries.append(
            {
                "name": child.name,
                "path": child.as_posix(),
                "hasAccountsDb": has_db,
                "accountsDbPath": db_path,
            }
        )
    has_db, db_path = _accounts_db_hint(resolved)
    return {
        "path": resolved.as_posix(),
        "parent": parent_path,
        "entries": entries,
        "hasAccountsDb": has_db,
        "accountsDbPath": db_path,
        "truncated": truncated,
    }


def discover_accounts_db(root: Path) -> Tuple[Path, List[Path]]:
    candidates: List[Path] = []
    preferred = root / DEFAULT_DB_RELATIVE_PATH
    if preferred.is_file():
        return preferred, [preferred]
    if root.is_dir():
        candidates = sorted(
            [p for p in root.rglob("accounts.sqlite") if p.is_file()],
            key=lambda p: (0 if DEFAULT_DB_RELATIVE_PATH.as_posix().lower() in p.as_posix().lower() else 1, str(p).lower()),
        )
    if not candidates:
        raise SDFarmError(f"Could not find accounts.sqlite under {root}")
    return candidates[0], candidates


def load_accounts(db_path: Path, limit: int = 5000) -> List[Dict[str, str]]:
    if not db_path.is_file():
        raise SDFarmError(f"SQLite database not found: {db_path}")
    max_rows = max(1, min(50000, int(limit or 5000)))
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT "
                + ", ".join([f'"{col}"' for col in ACCOUNT_COLUMNS])
                + ' FROM "accounts" ORDER BY "UID" LIMIT ?',
                (max_rows,),
            )
            rows = []
            for row in cur.fetchall():
                rows.append({col: "" if row[col] is None else str(row[col]) for col in ACCOUNT_COLUMNS})
            return rows
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise SDFarmError(f"Could not read SD Farm accounts: {e}") from e


def _ovpn_key(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    leaf = text.rsplit("/", 1)[-1]
    stem = leaf[:-5] if leaf.lower().endswith(".ovpn") else leaf
    return re.sub(r"\s+", "", stem).casefold()


def match_ovpn(openvpn: str, allowed_files: Iterable[str]) -> Tuple[str, str]:
    target = _ovpn_key(openvpn)
    if not target:
        return "", "missing_openvpn"
    matches = [item for item in allowed_files if _ovpn_key(item) == target]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        exact = [item for item in matches if Path(item).stem == str(openvpn).strip()]
        if len(exact) == 1:
            return exact[0], ""
        return "", "duplicate_ovpn"
    return "", "ovpn_not_found"


def route_username_for_uid(uid: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", str(uid or "")).strip("_").lower()
    return f"sd_{cleaned}" if cleaned else "sd_account"


def normalize_route_username(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(raw or "").lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return slug[:64].strip("_")


def validate_route_username(raw: str) -> Optional[str]:
    username = normalize_route_username(raw)
    if not username:
        return "route username is required"
    if len(username) < 2:
        return "route username is too short"
    return None


def resolve_route_username(uid: str, route_map: Optional[Dict[str, str]] = None) -> str:
    uid_text = str(uid or "").strip()
    custom = normalize_route_username(str((route_map or {}).get(uid_text) or ""))
    if custom:
        return custom
    return route_username_for_uid(uid_text)


def apply_route_map(rows: Iterable[Dict[str, Any]], route_map: Optional[Dict[str, str]] = None) -> None:
    mapping = route_map or {}
    for row in rows:
        uid = str(row.get("uid") or "").strip()
        row["routeUsername"] = resolve_route_username(uid, mapping)
        row["routeUsernameCustom"] = bool(uid and mapping.get(uid))


def export_route_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    exported: List[Dict[str, str]] = []
    for row in rows:
        uid = str(row.get("uid") or "").strip()
        if not uid:
            continue
        exported.append(
            {
                "uid": uid,
                "routeUsername": str(row.get("routeUsername") or resolve_route_username(uid, {})),
                "name": str(row.get("name") or "").strip(),
            }
        )
    return exported


def export_route_map_csv(rows: Iterable[Dict[str, Any]]) -> str:
    lines = ["uid,routeUsername,name"]
    for item in export_route_rows(rows):
        name = item["name"].replace('"', '""')
        lines.append(f'{item["uid"]},{item["routeUsername"]},"{name}"')
    return "\n".join(lines) + "\n"


def _parse_route_import_json(payload: Any) -> Tuple[Dict[str, str], List[str]]:
    mapping: Dict[str, str] = {}
    errors: List[str] = []
    if isinstance(payload, dict):
        routes = payload.get("routes")
        if isinstance(routes, list):
            for index, item in enumerate(routes, start=1):
                if not isinstance(item, dict):
                    errors.append(f"routes[{index - 1}] must be an object")
                    continue
                uid = str(item.get("uid") or "").strip()
                route = str(item.get("routeUsername") or item.get("route") or "").strip()
                if not uid:
                    errors.append(f"routes[{index - 1}] missing uid")
                    continue
                err = validate_route_username(route)
                if err:
                    errors.append(f"routes[{index - 1}] {err}")
                    continue
                mapping[uid] = normalize_route_username(route)
            return mapping, errors
        for uid, route in payload.items():
            if str(uid) in ("version", "exportedAt", "routes", "mode"):
                continue
            uid_text = str(uid).strip()
            route_text = str(route or "").strip()
            if not uid_text:
                continue
            err = validate_route_username(route_text)
            if err:
                errors.append(f"{uid_text}: {err}")
                continue
            mapping[uid_text] = normalize_route_username(route_text)
        return mapping, errors
    if isinstance(payload, list):
        return _parse_route_import_json({"routes": payload})
    errors.append("import payload must be a JSON object or routes array")
    return mapping, errors


def parse_route_import_text(text: str) -> Tuple[Dict[str, str], List[str]]:
    raw = str(text or "").strip()
    if not raw:
        return {}, ["import text is empty"]
    if raw.startswith("{") or raw.startswith("["):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            return {}, [f"invalid JSON: {e}"]
        return _parse_route_import_json(payload)

    mapping: Dict[str, str] = {}
    errors: List[str] = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("uid,") and "route" in stripped.lower():
            continue
        if "," in stripped:
            parts = [part.strip().strip('"') for part in stripped.split(",", 2)]
        elif "\t" in stripped:
            parts = [part.strip() for part in stripped.split("\t", 2)]
        else:
            parts = stripped.split(None, 1)
        if len(parts) < 2:
            errors.append(f"line {line_no}: expected uid and routeUsername")
            continue
        uid = parts[0].strip()
        route = parts[1].strip()
        err = validate_route_username(route)
        if not uid:
            errors.append(f"line {line_no}: missing uid")
            continue
        if err:
            errors.append(f"line {line_no}: {err}")
            continue
        mapping[uid] = normalize_route_username(route)
    return mapping, errors


def merge_route_map(
    existing: Dict[str, str],
    incoming: Dict[str, str],
    *,
    mode: str = "merge",
) -> Tuple[Dict[str, str], List[str]]:
    errors: List[str] = []
    route_to_uid: Dict[str, str] = {}
    for uid, route in incoming.items():
        prior = route_to_uid.get(route)
        if prior and prior != uid:
            errors.append(f"duplicate routeUsername '{route}' for uids {prior} and {uid}")
            continue
        route_to_uid[route] = uid

    base = {} if mode == "replace" else dict(existing or {})
    for uid, route in incoming.items():
        if route in route_to_uid and route_to_uid[route] != uid:
            continue
        base[uid] = route
    return base, errors


def _json_post(base_url: str, action: str, payload: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
    base = (base_url or "").rstrip("/") + "/"
    url = _ixbrowser_request_url(base + action.lstrip("/"))
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise IXBrowserError(f"ixBrowser request failed: {e}") from e
    except OSError as e:
        raise IXBrowserError(f"ixBrowser request failed: {e}") from e
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        raise IXBrowserError("ixBrowser returned invalid JSON") from e
    if not isinstance(data, dict):
        raise IXBrowserError("ixBrowser returned an unexpected response")
    error = data.get("error")
    if isinstance(error, dict) and int(error.get("code") or 0) != 0:
        raise IXBrowserError(str(error.get("message") or error.get("msg") or error))
    return data


def _response_data(raw: Dict[str, Any]) -> Any:
    data = raw.get("data")
    if isinstance(data, dict) and "data" in data:
        return data
    return data if data is not None else raw


def fetch_ixbrowser_profiles(
    base_url: str,
    page_limit: int = 100,
    *,
    timeout: float = 20.0,
) -> List[Dict[str, Any]]:
    limit = max(1, min(500, int(page_limit or 100)))
    page = 1
    profiles: List[Dict[str, Any]] = []
    total: Optional[int] = None
    while True:
        raw = _json_post(base_url, "profile-list", {"page": page, "limit": limit}, timeout=timeout)
        payload = _response_data(raw)
        if isinstance(payload, dict):
            if isinstance(payload.get("total"), int):
                total = int(payload["total"])
            items = payload.get("data")
        else:
            items = payload
        if not isinstance(items, list):
            raise IXBrowserError("ixBrowser profile-list response did not include a profile list")
        profiles.extend([item for item in items if isinstance(item, dict)])
        if len(items) < limit:
            break
        if total is not None and len(profiles) >= total:
            break
        page += 1
        if page > 1000:
            raise IXBrowserError("ixBrowser profile-list pagination did not finish")
    return profiles


def profile_id(profile: Dict[str, Any]) -> str:
    for key in ("profile_id", "id", "profileId"):
        value = profile.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def profile_name(profile: Dict[str, Any]) -> str:
    for key in ("name", "title", "profile_name"):
        value = profile.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def build_account_rows(
    accounts: Iterable[Dict[str, str]],
    allowed_ovpn_files: Iterable[str],
    profiles: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    files = list(allowed_ovpn_files)
    browser_profiles = list(profiles)
    rows: List[Dict[str, Any]] = []
    for account in accounts:
        uid = (account.get("UID") or "").strip()
        name = (account.get("Name") or "").strip()
        openvpn = (account.get("OpenVPN") or "").strip()
        matched_ovpn, ovpn_error = match_ovpn(openvpn, files)
        matches = [p for p in browser_profiles if uid and uid in profile_name(p)]
        warnings: List[str] = []
        browser_status = "matched"
        if not uid:
            browser_status = "missing_uid"
            warnings.append("Missing UID")
        elif len(matches) == 0:
            browser_status = "missing"
            warnings.append("No ixBrowser profile name contains this UID")
        elif len(matches) > 1:
            browser_status = "duplicate"
            warnings.append("Multiple ixBrowser profiles contain this UID")
        if ovpn_error:
            warnings.append(
                {
                    "missing_openvpn": "SD Farm OpenVPN value is empty",
                    "duplicate_ovpn": "Multiple Portico OVPN files match this SD Farm OpenVPN value",
                    "ovpn_not_found": "No Portico OVPN file matches this SD Farm OpenVPN value",
                }.get(ovpn_error, ovpn_error)
            )
        profile = matches[0] if len(matches) == 1 else {}
        route_username = route_username_for_uid(uid)
        valid = bool(uid and matched_ovpn and len(matches) == 1)
        rows.append(
            {
                "uid": uid,
                "name": name,
                "openvpn": openvpn,
                "proxy": account.get("Proxy") or "",
                "status": account.get("Status") or "",
                "currentStatus": account.get("Current_Status") or "",
                "matchedOvpn": matched_ovpn,
                "ovpnStatus": "matched" if matched_ovpn else (ovpn_error or "missing"),
                "browserStatus": browser_status,
                "browserProfileId": profile_id(profile) if profile else "",
                "browserProfileName": profile_name(profile) if profile else "",
                "routeUsername": route_username,
                "valid": valid,
                "warnings": warnings,
            }
        )
    return rows


def normalize_ixbrowser_proxy_type(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return "socks5" if value == "socks5" else "http"


def _coerce_ixbrowser_profile_id(profile_id_value: Any) -> int:
    text = str(profile_id_value or "").strip()
    if not text:
        raise IXBrowserError("Missing ixBrowser profile id")
    try:
        return int(text)
    except (TypeError, ValueError) as e:
        raise IXBrowserError(f"ixBrowser profile id must be numeric: {profile_id_value}") from e


def ovpn_note_from_matched_path(matched_ovpn: str) -> str:
    leaf = Path(str(matched_ovpn or "").replace("\\", "/")).name
    return leaf[:-5] if leaf.lower().endswith(".ovpn") else leaf


def update_ixbrowser_profile_proxy(
    base_url: str,
    profile_id_value: str,
    proxy_host: str,
    proxy_port: int,
    proxy_user: str,
    proxy_password: str,
    *,
    proxy_type: str = "http",
) -> Dict[str, Any]:
    profile_id_number = _coerce_ixbrowser_profile_id(profile_id_value)
    payload = {
        "profile_id": profile_id_number,
        "proxy_info": {
            "proxy_mode": 2,
            "proxy_type": normalize_ixbrowser_proxy_type(proxy_type),
            "proxy_ip": proxy_host,
            "proxy_port": int(proxy_port),
            "proxy_user": proxy_user,
            "proxy_password": proxy_password,
            "proxy_check_line": "global_line",
        },
    }
    raw = _json_post(base_url, "profile-update-proxy-for-custom-proxy", payload)
    return {"ok": True, "response": _response_data(raw)}


def update_ixbrowser_profile_note(
    base_url: str,
    profile_id_value: str,
    note: str,
) -> Dict[str, Any]:
    profile_id_number = _coerce_ixbrowser_profile_id(profile_id_value)
    payload = {
        "profile_id": profile_id_number,
        "note": str(note or "").strip(),
    }
    raw = _json_post(base_url, "profile-update", payload)
    return {"ok": True, "response": _response_data(raw)}


def sync_ixbrowser_profile(
    base_url: str,
    profile_id_value: str,
    proxy_host: str,
    proxy_port: int,
    proxy_user: str,
    proxy_password: str,
    matched_ovpn: str,
    *,
    proxy_type: str = "http",
) -> Dict[str, Any]:
    update_ixbrowser_profile_proxy(
        base_url,
        profile_id_value,
        proxy_host,
        proxy_port,
        proxy_user,
        proxy_password,
        proxy_type=proxy_type,
    )
    note = ovpn_note_from_matched_path(matched_ovpn)
    update_ixbrowser_profile_note(base_url, profile_id_value, note)
    return {"ok": True, "note": note}
