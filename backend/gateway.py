#!/usr/bin/env python3
"""
Portico — dynamic VPN proxy gateway: listens on one port per location (e.g. 50000, 50001, …),
runs at most maxSlots VPN+proxy pairs at a time, starts a proxy on-demand when a
client connects (holding the connection until ready), and shuts down proxies idle
for idleTimeoutMinutes (no proxy traffic; timer resets when bytes flow). HTTP proxy only (one port per location).
"""
from pathlib import Path
# Allow importing openvpn_proxy_runner when run from project root
_sys_path = Path(__file__).resolve().parent
if str(_sys_path) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_sys_path))

import argparse
import errno
import http.server
import json
import os
import secrets
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    import resource
except ImportError:
    resource = None  # Windows has no resource module

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

# Import runner for local backend (Docker backend imported when useDocker)
from ovpn_filter import (
    build_ovpn_country_options,
    filter_ovpn_files_by_country,
    filter_ovpn_files_by_query,
    infer_ovpn_country_code,
    normalize_randomize_country,
    randomize_country_status_label,
)
from openvpn_proxy_runner import resolve_ovpn_path, start_one_location
from provider_auth import load_provider_auth

BUFFER_SIZE = 65536  # 64 KB max buffer while waiting for backend
BACKEND_READY_TIMEOUT = 90  # seconds to wait for proxy (cap so client can retry if VPN is slow)
BACKEND_POLL_INTERVAL = 0.2  # check backend readiness 5x per second
BACKEND_CONNECT_TIMEOUT = 0.3  # socket timeout when probing backend (fail fast)
IDLE_CHECK_INTERVAL = 60  # seconds between idle eviction passes
INITIAL_READ_SELECT_TIMEOUT = 0.01  # 10ms: proceed almost instantly after first chunk
INITIAL_READ_DEADLINE = 0.5  # max seconds to wait for first byte (avoids long stall per connection)
PORTS_PER_LOCATION = 1  # One HTTP proxy port per location
BACKEND_HTTP_PORT = 8080
EXTEND_PORT_IDLE_SECONDS = 30 * 60  # user extend: add this much idle budget (monotonic last_activity)

listening_sockets: List[socket.socket] = []
shutdown_flag = False
CONTROL_PORT_DEFAULT = 49999
LOG_BUFFER_MAX = 1000
log_buffer: List[str] = []
DEFAULT_PROXY_USERNAME = "huzaifa"
DEFAULT_PROXY_PASSWORD = "huzaifa"
ALLOWED_ASSET_EXTENSIONS = {
    ".ovpn",
    ".crt",
    ".key",
    ".pem",
    ".p12",
    ".auth",
    ".txt",
}


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    line = f"[{ts}] [Gateway] {msg}"
    print(line, flush=True, file=sys.stderr)
    log_buffer.append(line)
    while len(log_buffer) > LOG_BUFFER_MAX:
        log_buffer.pop(0)


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def _enforce_default_proxy_auth(config: Dict[str, Any]) -> None:
    user = (config.get("proxyUsername") or "").strip()
    password = config.get("proxyPassword") or ""
    if not user or not password:
        config["proxyUsername"] = DEFAULT_PROXY_USERNAME
        config["proxyPassword"] = DEFAULT_PROXY_PASSWORD


def apply_openvpn_auth_env(config: Dict[str, Any]) -> None:
    """Override OpenVPN credentials from environment when variables are set (Docker-friendly)."""
    u = (os.environ.get("OPENVPN_USERNAME") or "").strip()
    p = (os.environ.get("OPENVPN_PASSWORD") or "").strip()

    # Only override when non-empty, to avoid accidentally wiping credentials.
    if u:
        config["username"] = u
        for loc in (config.get("locations") or []):
            if isinstance(loc, dict):
                loc["username"] = u

    if p:
        config["password"] = p
        for loc in (config.get("locations") or []):
            if isinstance(loc, dict):
                loc["password"] = p


def apply_location_spec(config: Dict[str, Any]) -> None:
    """
    If locationSpec is set, build config[\"locations\"] generically (no per-server list on disk).
    See README: locationSpec.count, defaultOvpn, labelPrefix, randomAccessFirstN.
    """
    spec = config.get("locationSpec")
    if not isinstance(spec, dict):
        return
    count = int(spec.get("count") or 0)
    default_ovpn = (spec.get("defaultOvpn") or "").strip()
    if count < 1:
        raise ValueError("locationSpec.count must be a positive integer")
    if not default_ovpn:
        raise ValueError(
            'locationSpec.defaultOvpn is required (path under ovpnRoot, e.g. "NC/NCVPN-US-Chicago-UDP.ovpn")'
        )
    prefix = (spec.get("labelPrefix") or "port").strip() or "port"
    random_n = max(0, int(spec.get("randomAccessFirstN") or 0))
    locations: List[Dict[str, Any]] = []
    for i in range(count):
        loc: Dict[str, Any] = {"label": f"{prefix}-{i}", "ovpn": default_ovpn}
        if i < random_n:
            loc["randomAccess"] = True
        locations.append(loc)
    config["locations"] = locations


def _locations_still_match_location_spec(config: Dict[str, Any]) -> bool:
    """True if locations[] is exactly what apply_location_spec would produce (safe to omit from disk)."""
    spec = config.get("locationSpec")
    if not isinstance(spec, dict):
        return False
    count = int(spec.get("count") or 0)
    default_ovpn = (spec.get("defaultOvpn") or "").strip()
    if count < 1 or not default_ovpn:
        return False
    prefix = (spec.get("labelPrefix") or "port").strip() or "port"
    random_n = max(0, int(spec.get("randomAccessFirstN") or 0))
    locs = config.get("locations") or []
    if len(locs) != count:
        return False
    for i, loc in enumerate(locs):
        if not isinstance(loc, dict):
            return False
        if (loc.get("ovpn") or "").strip() != default_ovpn:
            return False
        if (loc.get("label") or "") != f"{prefix}-{i}":
            return False
        ra = bool(loc.get("randomAccess"))
        if (i < random_n) != ra:
            return False
    return True


def _prepare_config_for_disk(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Persist compact locationSpec when the UI still reflects the generic template; otherwise
    save explicit locations (and drop locationSpec if the user customized rows).
    """
    if _locations_still_match_location_spec(config):
        return {k: v for k, v in config.items() if k != "locations"}
    if isinstance(config.get("locationSpec"), dict):
        return {k: v for k, v in config.items() if k != "locationSpec"}
    return config


def _is_safe_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _extract_referenced_assets(ovpn_path: Path) -> List[str]:
    directives = {"ca", "cert", "key", "tls-auth", "tls-crypt", "pkcs12", "auth-user-pass"}
    refs: List[str] = []
    for line in ovpn_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        parts = stripped.split()
        if not parts:
            continue
        key = parts[0].lower()
        if key not in directives:
            continue
        if len(parts) <= 1:
            continue
        val = parts[1].strip().strip('"').strip("'")
        if not val or val == "[inline]":
            continue
        refs.append(val)
    return refs


def _docker_ovpn_mount_path() -> Path:
    """Path inside gateway container where ovpn_data is mounted (must match workers' /ovpn)."""
    return Path(os.environ.get("DOCKER_OVPN_MOUNT", "/ovpn")).resolve()


def _is_safe_relative_ovpn_name(name: str) -> bool:
    p = Path((name or "").strip())
    if not str(p) or p.is_absolute():
        return False
    parts = p.parts
    if not parts:
        return False
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


def list_allowed_ovpn_files(config: Dict[str, Any], config_path: Path, use_docker: bool = False) -> List[str]:
    # Docker mode: list only files that actually exist on the shared volume (same as worker /ovpn).
    if use_docker:
        mount = _docker_ovpn_mount_path()
        if not mount.exists() or not mount.is_dir():
            _log(
                f"Docker OVPN mount missing or not a directory: {mount}. "
                "Mount ovpn_data at /ovpn on the gateway (see docker-compose.yml)."
            )
            return []
        files = sorted(
            str(p.relative_to(mount)).replace("\\", "/")
            for p in mount.rglob("*.ovpn")
            if p.is_file()
        )
        _log(f"Docker OVPN scan under {mount}: found {len(files)} .ovpn file(s)")
        if files:
            _log(f"Docker OVPN sample: {', '.join(files[:3])}")
        return files

    base_dir = config_path.resolve().parent
    ovpn_root = base_dir / config["ovpnRoot"] if config.get("ovpnRoot") else base_dir
    if not ovpn_root.exists() or not ovpn_root.is_dir():
        return []
    files: List[str] = []
    for p in ovpn_root.rglob("*.ovpn"):
        if p.is_file():
            files.append(str(p.relative_to(ovpn_root)).replace("\\", "/"))
    return sorted(files)


def build_ovpn_files_payload(
    config: Dict[str, Any], config_path: Path, use_docker: bool
) -> Dict[str, Any]:
    """Response body for GET /api/ovpn-files: file list plus diagnostics when empty or misconfigured."""
    files = list_allowed_ovpn_files(config, config_path, use_docker)
    payload: Dict[str, Any] = {
        "files": files,
        "countries": build_ovpn_country_options(files),
        "useDocker": use_docker,
        "ovpnCount": len(files),
        "unclassifiedOvpnCount": sum(1 for f in files if infer_ovpn_country_code(f) is None),
    }
    if use_docker:
        mount = _docker_ovpn_mount_path()
        path_exists = mount.exists()
        is_dir = mount.is_dir() if path_exists else False
        payload["scanPath"] = str(mount)
        payload["pathExists"] = path_exists
        payload["isDirectory"] = is_dir
        if not path_exists or not is_dir:
            payload["hint"] = (
                f"OVPN mount missing or not a directory at {mount}. "
                "Ensure docker-compose mounts ovpn_data at /ovpn on the gateway (see README)."
            )
        elif len(files) == 0:
            payload["hint"] = (
                "No .ovpn files under the gateway mount. Copy .ovpn files into the host folder set as "
                "OVPN_HOST_PATH. If you changed OVPN_HOST_PATH after the first `docker compose up`, "
                "Docker may still use the old volume: run `docker compose down`, `docker volume rm ovpn_data`, "
                "then `docker compose up -d`."
            )
    else:
        base_dir = config_path.resolve().parent
        ovpn_root = (base_dir / config["ovpnRoot"]).resolve() if config.get("ovpnRoot") else base_dir.resolve()
        path_exists = ovpn_root.exists()
        is_dir = ovpn_root.is_dir() if path_exists else False
        payload["scanPath"] = str(ovpn_root)
        payload["pathExists"] = path_exists
        payload["isDirectory"] = is_dir
        if not path_exists or not is_dir:
            payload["hint"] = (
                f"ovpnRoot does not exist or is not a directory: {ovpn_root}. "
                "Fix ovpnRoot in openvpn-proxy-config.json (relative to the config file directory)."
            )
        elif len(files) == 0:
            payload["hint"] = (
                f"No .ovpn files in {ovpn_root}. Add .ovpn files or update ovpnRoot in config."
            )
    return payload


def load_disk_config_expanded(config_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Load config JSON from disk and apply locationSpec (same as gateway startup / GET /api/config)."""
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg: Dict[str, Any] = json.load(f)
    except Exception as e:
        return None, f"Could not read config: {e}", 500
    try:
        apply_location_spec(cfg)
    except ValueError as e:
        return None, str(e), 400
    return cfg, None, 200


ASSIGNMENTS_STATE_VERSION = 1


def resolve_assignments_path(config_path: Path) -> Path:
    override = (os.environ.get("OPENVPN_PROXY_ASSIGNMENTS_PATH") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (config_path.parent / "openvpn-proxy-assignments.json").resolve()


def _redis_url_from_env_or_config(config: Optional[Dict[str, Any]] = None) -> str:
    u = (os.environ.get("REDIS_URL") or "").strip()
    if u:
        return u
    if config and isinstance(config.get("redisUrl"), str):
        return config["redisUrl"].strip()
    return ""


def _redis_state_key() -> str:
    return (os.environ.get("REDIS_STATE_KEY") or "portico:assignments-state").strip()


def _redis_load_json(url: str, key: str) -> Optional[Dict[str, Any]]:
    try:
        import redis as redis_mod  # type: ignore
    except ImportError:
        _log("redis package not installed; pip install redis")
        return None
    try:
        r = redis_mod.Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        raw = r.get(key)
        if not raw:
            return None
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception as e:
        _log(f"Redis GET {key!r} failed: {e}")
        return None


def _redis_save_json(url: str, key: str, payload: Dict[str, Any]) -> None:
    import redis as redis_mod  # type: ignore

    r = redis_mod.Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
    r.set(key, json.dumps(payload, ensure_ascii=False))


def assignments_state_payload(
    assignments: Dict[int, str],
    active_ports: Optional[Iterable[int]],
) -> Dict[str, Any]:
    ap_list = sorted(set(active_ports)) if active_ports is not None else []
    return {
        "version": ASSIGNMENTS_STATE_VERSION,
        "assignments": {str(p): name for p, name in sorted(assignments.items())},
        "activePorts": ap_list,
    }


def _parse_assignments_block(
    data: Dict[Any, Any],
    port_base: int,
    num_ports: int,
    allowed: Set[str],
    relaxed: bool,
) -> Dict[int, str]:
    """Parse assignments object from JSON. If relaxed, ignore allowed-set (still validate port + filename)."""
    out: Dict[int, str] = {}
    port_max = port_base + num_ports - 1
    skip_allowed_check = relaxed or len(allowed) == 0
    if not relaxed and len(allowed) == 0:
        _log(
            "No .ovpn files visible while loading assignments; restoring saved picks from disk "
            "without scan validation (verify OVPN mount if this persists)."
        )
    for k, v in data.items():
        try:
            port = int(str(k))
        except (TypeError, ValueError):
            continue
        if port < port_base or port > port_max:
            continue
        name = (v or "").strip() if isinstance(v, str) else ""
        if not name:
            continue
        if not _is_safe_relative_ovpn_name(name):
            _log(f"Skipping persisted assignment for port {port}: unsafe ovpn name {name!r}")
            continue
        if Path(name).suffix.lower() != ".ovpn":
            continue
        if not skip_allowed_check and name not in allowed:
            continue
        out[port] = name
    return out


def _ingest_assignments_raw(
    raw: Dict[str, Any],
    port_base: int,
    num_ports: int,
    runtime_config: Dict[str, Any],
    cfg_path: Path,
    use_docker: bool,
    source_label: str,
) -> Tuple[Dict[int, str], List[int]]:
    """Parse stored JSON blob (same shape as openvpn-proxy-assignments.json) into assignments + active ports."""
    assignments: Dict[int, str] = {}
    active_listener_ports: List[int] = []
    if num_ports <= 0:
        return assignments, active_listener_ports
    if isinstance(raw, dict) and isinstance(raw.get("assignments"), dict):
        data = raw["assignments"]
    elif isinstance(raw, dict):
        data = {k: v for k, v in raw.items() if str(k) not in ("version", "activePorts")}
    else:
        return assignments, active_listener_ports
    nkeys = len(data) if isinstance(data, dict) else 0
    allowed = set(list_allowed_ovpn_files(runtime_config, cfg_path, use_docker))
    _log(
        f"Loading assignments from {source_label} ({nkeys} raw port key(s), {len(allowed)} .ovpn file(s) visible to scan)"
    )

    assignments = _parse_assignments_block(data, port_base, num_ports, allowed, relaxed=False)
    if not assignments and nkeys > 0:
        assignments = _parse_assignments_block(data, port_base, num_ports, allowed, relaxed=True)
        if assignments:
            _log(
                f"Relaxed load restored {len(assignments)} assignment(s): saved filenames are not in the current "
                "OVPN scan (case mismatch, renamed files, or scan path). They will still show in the UI; activation may fail until fixed."
            )

    port_max = port_base + num_ports - 1
    raw_active = raw.get("activePorts") if isinstance(raw, dict) else None
    if isinstance(raw_active, list):
        for item in raw_active:
            try:
                p = int(item)
            except (TypeError, ValueError):
                continue
            if port_base <= p <= port_max:
                active_listener_ports.append(p)
    return assignments, sorted(set(active_listener_ports))


def load_gateway_assignments_state(
    path: Path,
    redis_url: str,
    redis_key: str,
    port_base: int,
    num_ports: int,
    runtime_config: Dict[str, Any],
    cfg_path: Path,
    use_docker: bool,
) -> Tuple[Dict[int, str], List[int]]:
    """Load OVPN picks + activePorts from Redis when configured, else JSON file; migrate file→Redis if needed."""
    if num_ports <= 0:
        return {}, []
    raw: Optional[Dict[str, Any]] = None
    source = ""
    loaded_from_redis = False
    if redis_url:
        raw = _redis_load_json(redis_url, redis_key)
        if raw is not None:
            loaded_from_redis = True
            source = f"redis key {redis_key!r}"
    if raw is None and path.exists() and path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            source = str(path)
        except Exception as e:
            _log(f"Could not load assignments file {path}: {e}")
            raw = None
    if raw is None:
        raw = {}
        source = source or "empty"
        if redis_url and not loaded_from_redis:
            _log(f"No assignments state in Redis ({redis_key!r}); no readable file at {path}")
        elif not redis_url and path.exists() and not path.is_file():
            _log(
                f"Assignments path is not a file (Docker may have created a directory): {path}. "
                "Remove it on the host and add a real JSON file, or set REDIS_URL."
            )
        elif not redis_url and not path.exists():
            _log(f"Assignments file does not exist yet: {path}")
    out = _ingest_assignments_raw(raw, port_base, num_ports, runtime_config, cfg_path, use_docker, source)
    # Redis had an empty document but legacy JSON still has rows — migrate once
    if (
        redis_url
        and loaded_from_redis
        and not out[0]
        and not out[1]
        and path.is_file()
    ):
        try:
            with open(path, encoding="utf-8") as f:
                file_raw = json.load(f)
            if (
                isinstance(file_raw, dict)
                and isinstance(file_raw.get("assignments"), dict)
                and file_raw["assignments"]
            ):
                _log(f"Migrating non-empty assignments from {path} into Redis")
                out = _ingest_assignments_raw(
                    file_raw, port_base, num_ports, runtime_config, cfg_path, use_docker, str(path)
                )
                try:
                    _redis_save_json(redis_url, redis_key, file_raw)
                except Exception as e:
                    _log(f"Redis migration save failed: {e}")
        except Exception as e:
            _log(f"Migration read from file failed: {e}")
    return out


def save_port_assignments_file(
    path: Path,
    assignments: Dict[int, str],
    active_ports: Optional[Iterable[int]] = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = assignments_state_payload(assignments, active_ports)
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def _anti_wipe_merge_assignments(
    state: Dict[str, Any],
    snap_assign: Dict[int, str],
    port_base: int,
    num_ports: int,
) -> Dict[int, str]:
    """If memory has no assignments, merge from Redis or JSON file so we do not persist an empty wipe."""
    if snap_assign:
        return snap_assign
    redis_url = (state.get("redis_url") or "").strip()
    redis_key = state.get("redis_state_key") or _redis_state_key()
    blk: Optional[Dict[Any, Any]] = None
    if redis_url:
        try:
            raw = _redis_load_json(redis_url, redis_key)
            if isinstance(raw, dict):
                blk = raw.get("assignments") if isinstance(raw.get("assignments"), dict) else None
        except Exception as e:
            _log(f"Persist anti-wipe Redis read failed: {e}")
    p = Path(state.get("assignments_path") or "")
    if blk is None and p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            blk = raw.get("assignments") if isinstance(raw, dict) else None
        except Exception as e:
            _log(f"Persist anti-wipe file read failed: {e}")
    if isinstance(blk, dict) and blk:
        disk_map = _parse_assignments_block(blk, port_base, num_ports, set(), relaxed=True)
        if disk_map:
            _log(f"Persist: in-memory assignments empty; merging {len(disk_map)} from storage (anti-wipe)")
            with state["lock"]:
                state["port_ovpn_assignment"].update(disk_map)
            return dict(state["port_ovpn_assignment"])
    return snap_assign


def persist_assignments_snapshot(state: Dict[str, Any]) -> None:
    path = state.get("assignments_path")
    if path is None:
        return
    p = Path(path)
    redis_url = (state.get("redis_url") or "").strip()
    redis_key = state.get("redis_state_key") or _redis_state_key()
    mirror_file = os.environ.get("REDIS_ASSIGNMENTS_MIRROR_FILE", "").lower() in ("1", "true", "yes")
    try:
        port_base = int(state["port_base"])
        num_ports = int(state.get("num_ports") or len(state.get("locations") or []))
        with state["lock"]:
            snap_assign = dict(state["port_ovpn_assignment"])
            snap_active = set(state["active_ports"])
        snap_assign = _anti_wipe_merge_assignments(state, snap_assign, port_base, num_ports)
        payload = assignments_state_payload(snap_assign, snap_active)
        if redis_url:
            try:
                _redis_save_json(redis_url, redis_key, payload)
            except Exception as e:
                _log(f"Could not persist assignments to Redis: {e}")
        if not redis_url:
            if p.exists() and not p.is_file():
                _log(f"Cannot persist assignments: path is not a file: {p}")
            else:
                save_port_assignments_file(p, snap_assign, snap_active)
        elif mirror_file:
            if p.exists() and not p.is_file():
                _log(f"REDIS_ASSIGNMENTS_MIRROR_FILE set but path is not a file: {p}")
            else:
                save_port_assignments_file(p, snap_assign, snap_active)
    except Exception as e:
        _log(f"Could not persist assignments: {e}")


def validate_location_assets(
    config: Dict[str, Any],
    config_path: Path,
    location_index: int,
    use_docker: bool = False,
    ovpn_override: Optional[str] = None,
) -> Optional[str]:
    locations = config.get("locations") or []
    if location_index < 0 or location_index >= len(locations):
        return f"location_index {location_index} out of range"

    loc = locations[location_index]
    ovpn_name = (ovpn_override or loc.get("ovpn") or "").strip()
    if not ovpn_name:
        return "Missing location ovpn filename"
    if Path(ovpn_name).suffix.lower() != ".ovpn":
        return f"Only .ovpn files are allowed. Got: {ovpn_name}"
    if use_docker:
        if not _is_safe_relative_ovpn_name(ovpn_name):
            return "In Docker mode, ovpn must be a safe relative path under /ovpn"
        mount = _docker_ovpn_mount_path()
        if not mount.exists() or not mount.is_dir():
            return (
                f"Gateway OVPN volume not mounted at {mount}. "
                "Add ovpn_data:/ovpn:ro to the gateway service in docker-compose.yml and restart."
            )
        ovpn_full = (mount / ovpn_name).resolve()
        if not _is_safe_under_root(ovpn_full, mount):
            return f"OVPN path escapes VPN volume: {ovpn_name}"
        if not ovpn_full.exists() or not ovpn_full.is_file():
            return (
                f"OVPN file not found in VPN folder: {ovpn_name}. "
                "Copy it into the host directory set as OVPN_HOST_PATH (or ../ovpn) and restart if needed."
            )
        if ovpn_full.suffix.lower() not in ALLOWED_ASSET_EXTENSIONS:
            return f"OVPN file extension not allowed: {ovpn_full.name}"
        try:
            refs = _extract_referenced_assets(ovpn_full)
        except OSError as e:
            return f"Failed to read OVPN file: {e}"
        for ref in refs:
            ref_path = (ovpn_full.parent / ref).resolve()
            if ref_path.suffix.lower() not in ALLOWED_ASSET_EXTENSIONS:
                return f"Referenced file extension not allowed: {ref}"
            if not _is_safe_under_root(ref_path, mount):
                return f"Referenced file escapes VPN volume: {ref}"
            if not ref_path.exists() or not ref_path.is_file():
                return f"Referenced OpenVPN asset missing: {ref}"
        try:
            load_provider_auth(ovpn_name, mount)
        except RuntimeError as e:
            return str(e)
        return None

    base_dir = config_path.resolve().parent
    ovpn_root = base_dir / config["ovpnRoot"] if config.get("ovpnRoot") else base_dir
    ovpn_full = resolve_ovpn_path(ovpn_name, ovpn_root, base_dir).resolve()
    if not ovpn_full.exists() or not ovpn_full.is_file():
        return f"OVPN file not found: {ovpn_name}"
    if not _is_safe_under_root(ovpn_full, ovpn_root):
        return f"OVPN file must be under ovpnRoot: {ovpn_root}"
    if ovpn_full.suffix.lower() not in ALLOWED_ASSET_EXTENSIONS:
        return f"OVPN file extension not allowed: {ovpn_full.name}"

    try:
        refs = _extract_referenced_assets(ovpn_full)
    except OSError as e:
        return f"Failed to read OVPN file: {e}"

    for ref in refs:
        ref_path = (ovpn_full.parent / ref).resolve()
        if ref_path.suffix.lower() not in ALLOWED_ASSET_EXTENSIONS:
            return f"Referenced file extension not allowed: {ref}"
        if not _is_safe_under_root(ref_path, ovpn_root):
            return f"Referenced file escapes ovpnRoot: {ref}"
        if not ref_path.exists() or not ref_path.is_file():
            return f"Referenced OpenVPN asset missing: {ref}"
    try:
        load_provider_auth(ovpn_name, ovpn_root)
    except RuntimeError as e:
        return str(e)
    return None


def _request_admin_rerun() -> None:
    if sys.platform != "win32":
        return
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except (AttributeError, OSError):
        return
    print("Requesting administrator privileges (approve the UAC prompt)...")
    lpFile = sys.executable
    lpParameters = subprocess.list2cmdline([str(Path(__file__).resolve())] + sys.argv[1:])
    lpDirectory = str(script_dir())
    SW_SHOWNORMAL = 1

    class SEE(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.DWORD),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hKeyClass", ctypes.c_void_p),
            ("dwHotKey", wintypes.DWORD),
            ("hMonitor", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    sei = SEE()
    sei.cbSize = ctypes.sizeof(SEE)
    sei.fMask = 0x40
    sei.lpVerb = "runas"
    sei.lpFile = lpFile
    sei.lpParameters = lpParameters
    sei.lpDirectory = lpDirectory
    sei.nShow = SW_SHOWNORMAL
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        print("Admin rights are required. Run as Administrator.", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def is_backend_running(slot: Dict[str, Any], use_docker: bool) -> bool:
    """True if the slot's backend is reachable (local process or docker container)."""
    if use_docker:
        host = slot.get("backend_host")
        port = slot.get("backend_port")
        if not host or not port:
            return False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(BACKEND_CONNECT_TIMEOUT)
            s.connect((host, port))
            s.close()
            return True
        except (socket.error, OSError):
            return False
    else:
        op = slot.get("openvpn_process")
        pp = slot.get("proxy_process")
        return op is not None and op.poll() is None and pp is not None and pp.poll() is None


def wait_for_backend(host: str, port: int, timeout_seconds: float = BACKEND_READY_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(BACKEND_CONNECT_TIMEOUT)
            s.connect((host, port))
            s.close()
            return True
        except (socket.error, OSError):
            pass
        time.sleep(BACKEND_POLL_INTERVAL)
    return False


def teardown_slot(slot: Dict[str, Any], use_docker: bool = False) -> None:
    """Terminate processes (local) or stop container (docker) for a slot."""
    ext = slot.get("external_port")
    if use_docker and slot.get("container_name"):
        _log(f"Teardown slot port={ext} container={slot['container_name']}")
        try:
            from backend_docker import teardown_docker_backend
            teardown_docker_backend(slot["container_name"])
        except Exception:
            pass
        slot["container_name"] = None
    else:
        _log(f"Teardown slot port={ext} (local processes)")
        for name, p in [("proxy", slot.get("proxy_process")), ("openvpn", slot.get("openvpn_process"))]:
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        for path_key in ("log_path", "auth_path"):
            path = slot.get(path_key)
            if path and path.strip():
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
        slot["openvpn_process"] = None
        slot["proxy_process"] = None
    slot["external_port"] = None
    slot["location_index"] = None


def forward(
    client_sock: socket.socket,
    backend_host: str,
    backend_port: int,
    initial_data: bytes,
    slot: Optional[Dict[str, Any]],
    lock: threading.Lock,
) -> None:
    """TCP proxy: client_sock <-> backend. Updates slot last_activity when slot and lock provided."""
    backend_sock = None
    try:
        backend_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        backend_sock.settimeout(300)
        backend_sock.connect((backend_host, backend_port))
        backend_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if initial_data:
            backend_sock.sendall(initial_data)
    except Exception:
        if backend_sock:
            try:
                backend_sock.close()
            except Exception:
                pass
        try:
            client_sock.close()
        except Exception:
            pass
        return

    client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    client_sock.settimeout(300)
    backend_sock.settimeout(300)

    def update_activity():
        if slot and lock:
            with lock:
                slot["last_activity"] = time.monotonic()

    def pump(a: socket.socket, b: socket.socket):
        try:
            data = a.recv(65536)
            if not data:
                return False
            b.sendall(data)
            update_activity()
            return True
        except (BlockingIOError, socket.error):
            return True
        except Exception:
            return False

    try:
        while True:
            r, _, _ = select.select([client_sock, backend_sock], [], [], 30)
            if not r:
                continue
            for s in r:
                if s is client_sock:
                    if not pump(client_sock, backend_sock):
                        return
                else:
                    if not pump(backend_sock, client_sock):
                        return
    except Exception:
        pass
    finally:
        try:
            client_sock.close()
        except Exception:
            pass
        try:
            backend_sock.close()
        except Exception:
            pass


def handle_connection(
    client_sock: socket.socket,
    external_port: int,
    config: dict,
    config_path: Path,
    port_base: int,
    internal_port_base: int,
    max_slots: int,
    slots: List[Dict[str, Any]],
    port_to_slot: Dict[int, Dict[str, Any]],
    active_ports: set,
    port_ovpn_assignment: Dict[int, str],
    activation_state_by_port: Dict[int, str],
    lock: threading.Lock,
    use_docker: bool = False,
    docker_image: str = "",
    docker_network: str = "proxynet",
    ovpn_volume_name: str = "ovpn_data",
) -> None:
    location_index = (external_port - port_base)
    locations = config.get("locations") or []
    _log(f"Connection on port {external_port} -> location_index={location_index}")
    if location_index < 0 or location_index >= len(locations):
        _log(f"Rejecting: location_index out of range (locations={len(locations)})")
        try:
            client_sock.close()
        except Exception:
            pass
        return

    with lock:
        state = activation_state_by_port.get(external_port, "inactive")
        if external_port not in active_ports or state != "active":
            _log(f"Rejecting connection on inactive port {external_port}")
            try:
                client_sock.close()
            except Exception:
                pass
            return
        assigned_ovpn = (port_ovpn_assignment.get(external_port) or "").strip()
        if not assigned_ovpn:
            _log(f"Rejecting connection on port {external_port}: no assigned ovpn file")
            try:
                client_sock.close()
            except Exception:
                pass
            return

    client_sock.settimeout(300)
    initial_data = b""
    try:
        client_sock.setblocking(False)
        deadline = time.monotonic() + INITIAL_READ_DEADLINE
        # Short select timeout so we proceed quickly after first chunk (avoids ~1s delay per request)
        while time.monotonic() < deadline and len(initial_data) < BUFFER_SIZE:
            r, _, _ = select.select([client_sock], [], [], INITIAL_READ_SELECT_TIMEOUT)
            if not r:
                continue
            try:
                chunk = client_sock.recv(65536)
                if not chunk:
                    break
                initial_data += chunk
                break
            except (BlockingIOError, socket.error):
                break
        client_sock.setblocking(True)
        client_sock.settimeout(300)
    except Exception:
        pass

    try:
        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass

    backend_port = BACKEND_HTTP_PORT
    with lock:
        slot = port_to_slot.get(external_port)
        if slot and is_backend_running(slot, use_docker):
            slot["last_activity"] = time.monotonic()
            backend_host = slot["backend_host"]
        elif slot and slot.get("location_index") == location_index:
            # Port reserved but backend not ready yet (another connection is starting it)
            backend_host = backend_port = None
        else:
            slot = None
            backend_host = backend_port = None
    if slot is not None and backend_host is not None and backend_port is not None:
        _log(f"Reusing existing slot for port {external_port} -> {backend_host}:{backend_port}")
        forward(client_sock, backend_host, backend_port, initial_data, slot, lock)
        return

    # Same location already claimed; wait for its backend to become ready
    if slot is not None and slot.get("location_index") == location_index:
        _log(f"Port {external_port} already allocated; waiting for backend to become ready")
        deadline = time.monotonic() + BACKEND_READY_TIMEOUT
        while time.monotonic() < deadline:
            with lock:
                bh, bp = slot.get("backend_host"), slot.get("backend_port")
            if bh and bp:
                if wait_for_backend(bh, bp):
                    with lock:
                        slot["last_activity"] = time.monotonic()
                    _log(f"Backend for port {external_port} became ready -> {bh}:{backend_port}")
                    forward(client_sock, bh, backend_port, initial_data, slot, lock)
                    return
                break
            time.sleep(BACKEND_POLL_INTERVAL)
        _log(f"Port {external_port} backend did not become ready in time")
        try:
            client_sock.close()
        except Exception:
            pass
        return

    # Need to start or assign a slot
    _log(f"Allocating slot for port {external_port} (location {location_index})")
    slot = None
    with lock:
        for s in slots:
            if s.get("external_port") is None:
                slot = s
                break
        if slot is None and len([s for s in slots if s.get("external_port") is not None]) >= max_slots:
            # Evict oldest (one port per slot)
            used = [s for s in slots if s.get("external_port") is not None]
            oldest = min(used, key=lambda s: s["last_activity"])
            old_loc = oldest["location_index"]
            old_port = port_base + old_loc
            _log(f"Max slots reached; evicting oldest slot location={old_loc} port={old_port}")
            port_to_slot.pop(old_port, None)
            teardown_slot(oldest, use_docker)
            slot = oldest
        if slot is None:
            # New slot
            internal_port = internal_port_base + len(slots)
            if len(slots) >= max_slots:
                try:
                    client_sock.close()
                except Exception:
                    pass
                return
            slot = {
                "internal_port": internal_port,
                "location_index": None,
                "openvpn_process": None,
                "proxy_process": None,
                "log_path": "",
                "auth_path": "",
                "backend_host": None,
                "backend_port": None,
                "container_name": None,
                "last_activity": time.monotonic(),
                "external_port": None,
            }
            slots.append(slot)
            _log(f"New slot allocated internal_port={slot['internal_port']}")
        # Reserve one port for this location
        first_port = port_base + location_index
        slot["location_index"] = location_index
        slot["external_port"] = first_port
        port_to_slot[first_port] = slot
        _log(f"Reserved port {first_port} for slot (internal_port={slot['internal_port']})")

    # Start backend for this location (outside lock to avoid blocking others)
    first_port = port_base + location_index
    launch_config = dict(config)
    launch_locations = [dict(loc) for loc in (config.get("locations") or [])]
    if 0 <= location_index < len(launch_locations):
        launch_locations[location_index]["ovpn"] = assigned_ovpn
    launch_config["locations"] = launch_locations
    if use_docker:
        _log(f"Starting Docker worker for location {location_index} port {first_port}")
        try:
            from backend_docker import start_docker_backend
            backend_host, _ = start_docker_backend(
                location_index, first_port, launch_config,
                docker_image, docker_network, ovpn_volume_name,
            )
            _log(f"Docker worker started: {backend_host} (HTTP:{BACKEND_HTTP_PORT})")
        except Exception as e:
            _log(f"Failed to start Docker worker for location {location_index}: {e}")
            try:
                client_sock.close()
            except Exception:
                pass
            with lock:
                slot["external_port"] = None
                slot["location_index"] = None
                port_to_slot.pop(first_port, None)
            return
        with lock:
            slot["backend_host"] = backend_host
            slot["backend_port"] = BACKEND_HTTP_PORT
            slot["container_name"] = backend_host
            slot["last_activity"] = time.monotonic()
        _log(f"Waiting for backend {backend_host}:{BACKEND_HTTP_PORT} (timeout={BACKEND_READY_TIMEOUT}s)")
        if not wait_for_backend(backend_host, BACKEND_HTTP_PORT):
            _log(f"Docker worker for location {location_index} did not become ready in time")
            cn = slot.get("container_name")
            if cn:
                try:
                    from backend_docker import get_worker_logs
                    logs = get_worker_logs(cn)
                    if logs:
                        for line in logs.strip().splitlines():
                            _log(f"Worker {cn} logs: {line}")
                        if "OVPN file not found" in logs or ("not found" in logs and "/ovpn/" in logs):
                            _log("Hint: Put .ovpn files in the host directory bound to ovpn_data (e.g. ./ovpn at repo root). See README Docker section.")
                except Exception:
                    pass
            teardown_slot(slot, use_docker)
            with lock:
                port_to_slot.pop(first_port, None)
                slot["external_port"] = None
                slot["location_index"] = None
            try:
                client_sock.close()
            except Exception:
                pass
            return
        _log(f"Forwarding port {external_port} -> {backend_host}:{backend_port}")
        forward(client_sock, backend_host, backend_port, initial_data, slot, lock)
    else:
        _log(f"Starting local backend for location {location_index} port {external_port} internal_port={slot['internal_port']}")
        try:
            openvpn_process, proxy_process, log_path, auth_path = start_one_location(
                launch_config, location_index, slot["internal_port"], config_path
            )
            _log(f"Local backend started for location {location_index}")
        except Exception as e:
            _log(f"Failed to start location {location_index}: {e}")
            try:
                client_sock.close()
            except Exception:
                pass
            with lock:
                slot["external_port"] = None
                slot["location_index"] = None
                port_to_slot.pop(first_port, None)
            return

        with lock:
            slot["openvpn_process"] = openvpn_process
            slot["proxy_process"] = proxy_process
            slot["log_path"] = log_path
            slot["auth_path"] = auth_path
            slot["backend_host"] = "127.0.0.1"
            slot["backend_port"] = slot["internal_port"]
            slot["last_activity"] = time.monotonic()

        _log(f"Waiting for local backend 127.0.0.1:{slot['internal_port']}")
        if not wait_for_backend("127.0.0.1", slot["internal_port"]):
            _log(f"Local proxy for location {location_index} did not become ready in time")
            teardown_slot(slot, use_docker)
            with lock:
                port_to_slot.pop(first_port, None)
                slot["external_port"] = None
                slot["location_index"] = None
            try:
                client_sock.close()
            except Exception:
                pass
            return

        _log(f"Forwarding port {external_port} -> 127.0.0.1:{slot['internal_port']}")
        forward(client_sock, "127.0.0.1", slot["internal_port"], initial_data, slot, lock)


def idle_eviction_loop(
    state: Dict[str, Any],
    idle_timeout_seconds: float,
    use_docker: bool = False,
    port_base: int = 50000,
) -> None:
    global shutdown_flag
    slots: List[Dict[str, Any]] = state["slots"]
    port_to_slot: Dict[int, Dict[str, Any]] = state["port_to_slot"]
    lock = state["lock"]
    while not shutdown_flag:
        time.sleep(IDLE_CHECK_INTERVAL)
        if shutdown_flag:
            break
        now = time.monotonic()
        with lock:
            to_evict = []
            for slot in slots:
                if slot.get("external_port") is not None and (now - slot.get("last_activity", 0)) > idle_timeout_seconds:
                    to_evict.append(slot)
            evicted_ports = [port_base + s["location_index"] for s in to_evict]
            for slot in to_evict:
                loc = slot["location_index"]
                port_to_slot.pop(port_base + loc, None)
                teardown_slot(slot, use_docker)
                slot["external_port"] = None
                slot["location_index"] = None
            for ep in evicted_ports:
                state["active_ports"].discard(ep)
                state["activation_state_by_port"][ep] = "inactive"
                state["activation_error_by_port"].pop(ep, None)
            if to_evict:
                _log(f"Evicted {len(to_evict)} idle slot(s): {evicted_ports}")
        if to_evict:
            persist_assignments_snapshot(state)


def _start_backend_for_port_now(
    port: int,
    config: Dict[str, Any],
    config_path: Path,
    port_base: int,
    internal_port_base: int,
    max_slots: int,
    slots: List[Dict[str, Any]],
    port_to_slot: Dict[int, Dict[str, Any]],
    port_ovpn_assignment: Dict[int, str],
    lock: threading.Lock,
    use_docker: bool = False,
    docker_image: str = "",
    docker_network: str = "proxynet",
    ovpn_volume_name: str = "ovpn_data",
) -> Optional[str]:
    location_index = port - port_base
    locations = config.get("locations") or []
    if location_index < 0 or location_index >= len(locations):
        return "Port out of location range"

    assigned_ovpn = (port_ovpn_assignment.get(port) or "").strip()
    if not assigned_ovpn:
        return "Select an OVPN file for this port before activation"

    with lock:
        existing = port_to_slot.get(port)
        if existing and is_backend_running(existing, use_docker):
            existing["last_activity"] = time.monotonic()
            return None

    # Allocate/reuse slot
    slot = None
    with lock:
        existing = port_to_slot.get(port)
        if existing and existing.get("location_index") == location_index:
            slot = existing
        if slot is None:
            for s in slots:
                if s.get("external_port") is None:
                    slot = s
                    break
        if slot is None and len([s for s in slots if s.get("external_port") is not None]) >= max_slots:
            used = [s for s in slots if s.get("external_port") is not None]
            oldest = min(used, key=lambda s: s["last_activity"])
            old_loc = oldest["location_index"]
            old_port = port_base + old_loc
            _log(f"Max slots reached; evicting oldest slot location={old_loc} port={old_port}")
            port_to_slot.pop(old_port, None)
            teardown_slot(oldest, use_docker)
            slot = oldest
        if slot is None:
            if len(slots) >= max_slots:
                return "No available slot capacity"
            slot = {
                "internal_port": internal_port_base + len(slots),
                "location_index": None,
                "openvpn_process": None,
                "proxy_process": None,
                "log_path": "",
                "auth_path": "",
                "backend_host": None,
                "backend_port": None,
                "container_name": None,
                "last_activity": time.monotonic(),
                "external_port": None,
            }
            slots.append(slot)
        slot["location_index"] = location_index
        slot["external_port"] = port
        port_to_slot[port] = slot

    launch_config = dict(config)
    launch_locations = [dict(loc) for loc in (config.get("locations") or [])]
    launch_locations[location_index]["ovpn"] = assigned_ovpn
    launch_config["locations"] = launch_locations

    if use_docker:
        try:
            from backend_docker import start_docker_backend
            backend_host, _ = start_docker_backend(
                location_index, port, launch_config, docker_image, docker_network, ovpn_volume_name
            )
        except Exception as e:
            with lock:
                slot["external_port"] = None
                slot["location_index"] = None
                port_to_slot.pop(port, None)
            return f"Failed to start Docker worker: {e}"
        with lock:
            slot["backend_host"] = backend_host
            slot["backend_port"] = BACKEND_HTTP_PORT
            slot["container_name"] = backend_host
            slot["last_activity"] = time.monotonic()
        if not wait_for_backend(backend_host, BACKEND_HTTP_PORT):
            teardown_slot(slot, use_docker)
            with lock:
                slot["external_port"] = None
                slot["location_index"] = None
                port_to_slot.pop(port, None)
            return "Docker worker did not become ready in time"
        return None

    try:
        openvpn_process, proxy_process, log_path, auth_path = start_one_location(
            launch_config, location_index, slot["internal_port"], config_path
        )
    except Exception as e:
        with lock:
            slot["external_port"] = None
            slot["location_index"] = None
            port_to_slot.pop(port, None)
        return f"Failed to start location: {e}"

    with lock:
        slot["openvpn_process"] = openvpn_process
        slot["proxy_process"] = proxy_process
        slot["log_path"] = log_path
        slot["auth_path"] = auth_path
        slot["backend_host"] = "127.0.0.1"
        slot["backend_port"] = slot["internal_port"]
        slot["last_activity"] = time.monotonic()

    if not wait_for_backend("127.0.0.1", slot["internal_port"]):
        teardown_slot(slot, use_docker)
        with lock:
            slot["external_port"] = None
            slot["location_index"] = None
            port_to_slot.pop(port, None)
        return "Local backend did not become ready in time"
    return None


def _activate_port_async(
    port: int,
    runtime_config: Dict[str, Any],
    state: Dict[str, Any],
) -> None:
    start_err = _start_backend_for_port_now(
        port=port,
        config=runtime_config,
        config_path=state["config_path"],
        port_base=state["port_base"],
        internal_port_base=runtime_config.get("internalPortBase", 51000),
        max_slots=state["max_slots"],
        slots=state["slots"],
        port_to_slot=state["port_to_slot"],
        port_ovpn_assignment=state["port_ovpn_assignment"],
        lock=state["lock"],
        use_docker=bool(state.get("use_docker")),
        docker_image=runtime_config.get("dockerImage") or os.environ.get("DOCKER_IMAGE", "portico-worker"),
        docker_network=runtime_config.get("dockerNetwork") or os.environ.get("DOCKER_NETWORK", "proxynet"),
        ovpn_volume_name=runtime_config.get("dockerOvpnVolume") or os.environ.get("DOCKER_OVPN_VOLUME", "ovpn_data"),
    )

    lock = state["lock"]
    with lock:
        cancelled = port in state["activation_cancelled_ports"]
        if cancelled:
            state["activation_cancelled_ports"].discard(port)

    if start_err:
        with lock:
            state["active_ports"].discard(port)
            state["activation_state_by_port"][port] = "failed"
            state["activation_error_by_port"][port] = str(start_err)
        persist_assignments_snapshot(state)
        return

    if cancelled:
        with lock:
            slot = state["port_to_slot"].get(port)
            if slot is not None:
                loc = slot.get("location_index")
                if loc is not None:
                    state["port_to_slot"].pop(state["port_base"] + loc, None)
                teardown_slot(slot, state["use_docker"])
                slot["external_port"] = None
                slot["location_index"] = None
            state["active_ports"].discard(port)
            state["activation_state_by_port"][port] = "inactive"
            state["activation_error_by_port"].pop(port, None)
        persist_assignments_snapshot(state)
        return

    with lock:
        state["activation_state_by_port"][port] = "active"
        state["activation_error_by_port"].pop(port, None)
    persist_assignments_snapshot(state)


def _control_api_handler_factory(
    gui_dir: Path,
    state: Dict[str, Any],
) -> type:
    """Build a request handler class that closes over gui_dir and state."""

    class GatewayControlHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass  # suppress default request logging

        def _send_json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Client closed connection before response was written.
                return

        def _send_error_body(self, message: str, status: int = 400) -> None:
            self._send_json({"error": message}, status=status)

        def _serve_file(self, path: Path, content_type: str) -> bool:
            if not path.is_file():
                return False
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return False
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/api/status":
                self._handle_get_status()
            elif path == "/api/config":
                self._handle_get_config()
            elif path == "/api/ovpn-files":
                self._handle_get_ovpn_files()
            elif path == "/api/logs":
                self._handle_get_logs(parsed.query)
            elif path == "/api/worker-logs":
                self._handle_get_worker_logs(parsed.query)
            elif path == "/api/test-proxy":
                self._handle_get_test_proxy(parsed.query)
            else:
                self.send_error(404)

        def _handle_get_status(self) -> None:
            lock = state["lock"]
            port_to_slot = state["port_to_slot"]
            active_ports = state["active_ports"]
            port_ovpn_assignment = state["port_ovpn_assignment"]
            activation_state_by_port = state["activation_state_by_port"]
            activation_error_by_port = state["activation_error_by_port"]
            locations = state["locations"]
            port_base = state["port_base"]
            num_ports = state.get("num_ports") or len(locations)
            now = time.monotonic()
            with lock:
                active = []
                for port, slot in list(port_to_slot.items()):
                    loc_idx = slot.get("location_index")
                    if loc_idx is None:
                        continue
                    label = locations[loc_idx].get("label", "") if loc_idx < len(locations) else ""
                    last = slot.get("last_activity") or 0
                    age_seconds = max(0.0, now - last) if last else 0.0
                    entry = {
                        "port": port,
                        "locationIndex": loc_idx,
                        "locationLabel": label,
                        "lastActivityAgeSeconds": round(age_seconds, 1),
                        "proxyType": "http",
                    }
                    if state.get("use_docker") and slot.get("container_name"):
                        entry["containerName"] = slot["container_name"]
                    active.append(entry)
                enabled_ports = sorted(list(active_ports))
                assigned_by_port = {
                    str(p): (port_ovpn_assignment.get(p) or "").strip()
                    for p in range(port_base, port_base + num_ports)
                }
                activation_state = {str(k): v for k, v in activation_state_by_port.items()}
                activation_error = {str(k): v for k, v in activation_error_by_port.items()}
            port_max = port_base + num_ports - 1
            listen_h = state.get("listen_host", "127.0.0.1") or "127.0.0.1"
            randomize_country = "random"
            randomize_country_pool = "any country"
            cfg_client = ""
            try:
                with open(state["config_path"], encoding="utf-8") as _cf:
                    _cfg = json.load(_cf)
                randomize_country = normalize_randomize_country(_cfg.get("randomizeCountry"))
                randomize_country_pool = randomize_country_status_label(_cfg.get("randomizeCountry"))
                cfg_client = (str(_cfg.get("clientProxyHost") or "")).strip()
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
            if cfg_client:
                client_proxy_host = cfg_client
            elif listen_h in ("0.0.0.0", "::", "[::]"):
                client_proxy_host = "127.0.0.1"
            else:
                client_proxy_host = listen_h
            self._send_json({
                "running": True,
                "portBase": state["port_base"],
                "publishedPortBase": state.get("published_port_base"),
                "maxSlots": state["max_slots"],
                "idleTimeoutMinutes": state["idle_timeout_minutes"],
                "useDocker": state["use_docker"],
                "listenHost": listen_h,
                "clientProxyHost": client_proxy_host,
                "proxyUsername": state.get("proxy_username") or "",
                "proxyPassword": state.get("proxy_password") or "",
                "controlPort": state.get("control_port", 0),
                "totalPorts": num_ports,
                "portMax": port_max,
                "enabledPorts": enabled_ports,
                "assignedOvpnByPort": assigned_by_port,
                "activationStateByPort": activation_state,
                "activationErrorByPort": activation_error,
                "locations": [
                    {
                        "label": loc.get("label", ""),
                        "ovpn": loc.get("ovpn", ""),
                        "randomAccess": bool(loc.get("randomAccess")),
                    }
                    for loc in locations
                ],
                "activeSlots": active,
                "randomizeCountry": randomize_country,
                "randomizeCountryPool": randomize_country_pool,
            })

        def _handle_get_config(self) -> None:
            config_path = state["config_path"]
            if not config_path.exists():
                self._send_error_body("Config file not found", 404)
                return
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                apply_location_spec(config)
            except ValueError as e:
                self._send_error_body(str(e), 400)
                return
            except Exception as e:
                self._send_error_body(str(e), 500)
                return
            self._send_json(config)

        def _handle_get_ovpn_files(self) -> None:
            config_path = state["config_path"]
            runtime_config, load_err, load_status = load_disk_config_expanded(config_path)
            if load_err:
                self._send_error_body(load_err, load_status)
                return
            payload = build_ovpn_files_payload(
                runtime_config,
                config_path,
                bool(state.get("use_docker")),
            )
            self._send_json(payload)

        def _handle_get_logs(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            tail = 200
            if params.get("tail"):
                try:
                    tail = min(1000, max(1, int(params["tail"][0])))
                except (ValueError, IndexError):
                    pass
            lines = log_buffer[-tail:] if log_buffer else []
            self._send_json({"lines": lines})

        def _handle_get_worker_logs(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            ports = params.get("port", [])
            if not ports:
                self._send_error_body("Missing port", 400)
                return
            try:
                port = int(ports[0])
            except ValueError:
                self._send_error_body("Invalid port", 400)
                return
            port_base = state["port_base"]
            num_ports = state.get("num_ports") or 0
            if port < port_base or port >= (port_base + num_ports):
                self._send_error_body("Port out of range", 400)
                return
            lock = state["lock"]
            port_to_slot = state["port_to_slot"]
            use_docker = state.get("use_docker", False)
            with lock:
                slot = port_to_slot.get(port)
            if not slot or not use_docker:
                self._send_error_body("No Docker worker for port", 404)
                return
            container_name = slot.get("container_name")
            if not container_name:
                self._send_error_body("No container for slot", 404)
                return
            try:
                from backend_docker import get_worker_logs
                logs = get_worker_logs(container_name)
            except Exception as e:
                self._send_error_body(str(e), 500)
                return
            if logs is None:
                self._send_error_body("Could not get logs", 404)
                return
            self._send_json({"logs": logs})

        def _handle_get_test_proxy(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            ports = params.get("port", [])
            if not ports:
                self._send_error_body("Missing port", 400)
                return
            try:
                port = int(ports[0])
            except ValueError:
                self._send_error_body("Invalid port", 400)
                return
            listen_host = state.get("listen_host", "127.0.0.1")
            proxy_user = state.get("proxy_username") or ""
            proxy_pass = state.get("proxy_password") or ""
            if proxy_user and proxy_pass:
                user_enc = urllib.parse.quote(proxy_user, safe="")
                pass_enc = urllib.parse.quote(proxy_pass, safe="")
                proxy_url = f"http://{user_enc}:{pass_enc}@{connect_host}:{port}"
            else:
                proxy_url = f"http://{connect_host}:{port}"
            try:
                import urllib.request as urllib_request
                proxy_handler = urllib_request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                opener = urllib_request.build_opener(proxy_handler)
                req = urllib_request.Request("https://api.ipify.org?format=json", headers={"User-Agent": "OpenVPN-Proxy-Gateway/1.0"})
                with opener.open(req, timeout=15) as resp:
                    body = resp.read().decode("utf-8")
                match = re.search(r'"ip"\s*:\s*"([^"]+)"', body) if body else None
                exit_ip = match.group(1) if match else body.strip()
                self._send_json({"ok": True, "exitIp": exit_ip})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/")
            if path == "/api/config":
                self._handle_post_config()
            elif path == "/api/assign-ovpn":
                self._handle_post_assign_ovpn(parsed.query)
            elif path == "/api/activate":
                self._handle_post_activate(parsed.query)
            elif path == "/api/deactivate":
                self._handle_post_deactivate(parsed.query)
            elif path == "/api/randomize-port":
                self._handle_post_randomize_port(parsed.query)
            elif path == "/api/refresh-port":
                self._handle_post_refresh_port(parsed.query)
            elif path == "/api/extend-port":
                self._handle_post_extend_port(parsed.query)
            elif path == "/api/shutdown":
                self._handle_post_shutdown()
            elif path == "/api/evict":
                self._handle_post_evict(parsed.query)
            else:
                self.send_error(404)

        def _handle_post_config(self) -> None:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0 or content_length > 2 * 1024 * 1024:
                self._send_error_body("Invalid Content-Length", 400)
                return
            try:
                body = self.rfile.read(content_length).decode("utf-8")
                config = json.loads(body)
            except Exception as e:
                self._send_error_body(str(e), 400)
                return
            try:
                apply_location_spec(config)
            except ValueError as e:
                self._send_error_body(str(e), 400)
                return
            if not isinstance(config.get("locations"), list):
                self._send_error_body("config.locations must be an array (or set locationSpec)", 400)
                return
            _enforce_default_proxy_auth(config)
            to_save = _prepare_config_for_disk(config)
            config_path = state["config_path"]
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(to_save, f, indent=2)
            except OSError as e:
                if getattr(e, "errno", None) == errno.EROFS or "read-only" in str(e).lower():
                    self._send_error_body(
                        "Config file is read-only. When using Docker, remove :ro from the config volume in docker-compose.yml or edit the file on the host and restart.",
                        503,
                    )
                else:
                    self._send_error_body(str(e), 500)
                return
            self._send_json({"ok": True, "message": "Config saved. Restart the gateway to apply."})

        def _handle_post_shutdown(self) -> None:
            global shutdown_flag
            shutdown_flag = True
            self._send_json({"ok": True})

        def _handle_post_assign_ovpn(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            ports = params.get("port", [])
            if not ports:
                self._send_error_body("Missing port", 400)
                return
            try:
                port = int(ports[0])
            except ValueError:
                self._send_error_body("Invalid port", 400)
                return
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0 or content_length > 16 * 1024:
                self._send_error_body("Invalid Content-Length", 400)
                return
            try:
                body = self.rfile.read(content_length).decode("utf-8")
                payload = json.loads(body)
            except Exception as e:
                self._send_error_body(str(e), 400)
                return
            ovpn = (payload.get("ovpn") or "").strip()
            port_base = state["port_base"]
            num_ports = state.get("num_ports") or len(state["locations"])
            if port < port_base or port >= port_base + num_ports:
                self._send_error_body("Port out of location range", 400)
                return

            if not ovpn:
                with state["lock"]:
                    state["port_ovpn_assignment"].pop(port, None)
                persist_assignments_snapshot(state)
                self._send_json({"ok": True, "port": port, "ovpn": ""})
                return

            if Path(ovpn).suffix.lower() != ".ovpn":
                self._send_error_body("Only .ovpn files are allowed", 400)
                return
            if not _is_safe_relative_ovpn_name(ovpn):
                self._send_error_body("ovpn must be a safe relative path", 400)
                return
            try:
                with open(state["config_path"], encoding="utf-8") as f:
                    runtime_config = json.load(f)
            except Exception as e:
                self._send_error_body(f"Could not read config: {e}", 500)
                return
            allowed = list_allowed_ovpn_files(
                runtime_config,
                state["config_path"],
                bool(state.get("use_docker")),
            )
            if ovpn not in allowed:
                self._send_error_body("Selected ovpn is not in allowed list", 400)
                return

            with state["lock"]:
                state["port_ovpn_assignment"][port] = ovpn
            persist_assignments_snapshot(state)
            self._send_json({"ok": True, "port": port, "ovpn": ovpn})

        def _handle_post_activate(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            ports = params.get("port", [])
            if not ports:
                self._send_error_body("Missing port", 400)
                return
            try:
                port = int(ports[0])
            except ValueError:
                self._send_error_body("Invalid port", 400)
                return

            port_base = state["port_base"]
            locations = state["locations"]
            loc_idx = port - port_base
            if loc_idx < 0 or loc_idx >= len(locations):
                self._send_error_body("Port out of location range", 400)
                return

            config_path = state["config_path"]
            runtime_config, load_err, load_status = load_disk_config_expanded(config_path)
            if load_err:
                self._send_error_body(load_err, load_status)
                return
            _enforce_default_proxy_auth(runtime_config)
            apply_openvpn_auth_env(runtime_config)
            assigned_ovpn = ""
            with state["lock"]:
                assigned_ovpn = (state["port_ovpn_assignment"].get(port) or "").strip()
            if not assigned_ovpn:
                self._send_error_body("Select an OVPN file for this port before activation", 400)
                return
            err = validate_location_assets(
                runtime_config,
                config_path,
                loc_idx,
                bool(state.get("use_docker")),
                assigned_ovpn,
            )
            if err:
                self._send_error_body(err, 400)
                return

            with state["lock"]:
                current_state = state["activation_state_by_port"].get(port, "inactive")
                if current_state == "starting":
                    self._send_json({"ok": True, "port": port, "locationIndex": loc_idx, "activationState": "starting"})
                    return
                if current_state == "active":
                    self._send_json({"ok": True, "port": port, "locationIndex": loc_idx, "activationState": "active"})
                    return
                state["activation_cancelled_ports"].discard(port)
                state["active_ports"].add(port)
                state["activation_state_by_port"][port] = "starting"
                state["activation_error_by_port"].pop(port, None)

            threading.Thread(
                target=_activate_port_async,
                args=(port, runtime_config, state),
                daemon=True,
            ).start()
            persist_assignments_snapshot(state)
            self._send_json({"ok": True, "port": port, "locationIndex": loc_idx, "activationState": "starting"})

        def _handle_post_deactivate(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            ports = params.get("port", [])
            if not ports:
                self._send_error_body("Missing port", 400)
                return
            try:
                port = int(ports[0])
            except ValueError:
                self._send_error_body("Invalid port", 400)
                return

            lock = state["lock"]
            port_to_slot = state["port_to_slot"]
            port_base = state["port_base"]
            use_docker = state["use_docker"]
            with lock:
                state["active_ports"].discard(port)
                state["activation_cancelled_ports"].add(port)
                state["activation_state_by_port"][port] = "inactive"
                state["activation_error_by_port"].pop(port, None)
                slot = port_to_slot.get(port)
                if slot is not None:
                    loc = slot.get("location_index")
                    if loc is not None:
                        port_to_slot.pop(port_base + loc, None)
                    teardown_slot(slot, use_docker)
                    slot["external_port"] = None
                    slot["location_index"] = None
            persist_assignments_snapshot(state)
            self._send_json({"ok": True, "port": port})

        def _handle_post_extend_port(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            ports = params.get("port", [])
            if not ports:
                self._send_error_body("Missing port", 400)
                return
            try:
                port = int(ports[0])
            except ValueError:
                self._send_error_body("Invalid port", 400)
                return

            port_base = state["port_base"]
            num_ports = state.get("num_ports") or len(state["locations"])
            if port < port_base or port >= port_base + num_ports:
                self._send_error_body("Port out of location range", 400)
                return

            use_docker = state["use_docker"]
            lock = state["lock"]
            port_to_slot = state["port_to_slot"]
            now = time.monotonic()
            with lock:
                if state["activation_state_by_port"].get(port) != "active":
                    self._send_error_body("Port is not active", 400)
                    return
                slot = port_to_slot.get(port)
                if slot is None or slot.get("external_port") is None:
                    self._send_error_body("No running slot for this port", 400)
                    return
                if not is_backend_running(slot, use_docker):
                    self._send_error_body("Backend is not running", 503)
                    return
                slot["last_activity"] = slot.get("last_activity", now) + EXTEND_PORT_IDLE_SECONDS
                last = slot.get("last_activity") or 0
                age_seconds = max(0.0, now - last)
            self._send_json({"ok": True, "port": port, "lastActivityAgeSeconds": round(age_seconds, 1)})

        def _handle_post_randomize_port(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            ports = params.get("port", [])
            if not ports:
                self._send_error_body("Missing port", 400)
                return
            try:
                port = int(ports[0])
            except ValueError:
                self._send_error_body("Invalid port", 400)
                return

            port_base = state["port_base"]
            num_ports = state.get("num_ports") or len(state["locations"])
            locations = state["locations"]
            loc_idx = port - port_base
            if loc_idx < 0 or loc_idx >= len(locations):
                self._send_error_body("Port out of location range", 400)
                return
            loc = locations[loc_idx]
            if not bool(loc.get("randomAccess")):
                self._send_error_body("Port is not a random-access slot", 403)
                return

            config_path = state["config_path"]
            runtime_config, load_err, load_status = load_disk_config_expanded(config_path)
            if load_err:
                self._send_error_body(load_err, load_status)
                return
            _enforce_default_proxy_auth(runtime_config)
            apply_openvpn_auth_env(runtime_config)

            allowed = list_allowed_ovpn_files(
                runtime_config,
                config_path,
                bool(state.get("use_docker")),
            )
            if not allowed:
                self._send_error_body("No .ovpn files available to assign", 400)
                return

            rc = normalize_randomize_country(runtime_config.get("randomizeCountry"))
            if rc != "random":
                allowed = filter_ovpn_files_by_country(allowed, rc)
                if not allowed:
                    self._send_error_body(
                        f"No .ovpn files for country {rc} (randomizeCountry). "
                        "Add matching profiles or set randomizeCountry to random.",
                        400,
                    )
                    return

            filter_str = ""
            try:
                cl = int(self.headers.get("Content-Length", 0) or 0)
                if 0 < cl <= 8192:
                    raw_body = self.rfile.read(cl).decode("utf-8")
                    body_obj = json.loads(raw_body)
                    if isinstance(body_obj, dict):
                        filter_str = (body_obj.get("filter") or "").strip()
            except (json.JSONDecodeError, OSError, UnicodeDecodeError, TypeError, ValueError):
                filter_str = ""

            if filter_str:
                pool = filter_ovpn_files_by_query(allowed, filter_str)
                if not pool:
                    self._send_error_body(
                        "No .ovpn files match the randomize filter; try different search terms",
                        400,
                    )
                    return
            else:
                pool = list(allowed)

            lock = state["lock"]
            with lock:
                current = (state["port_ovpn_assignment"].get(port) or "").strip()

            pool = list(pool)
            if len(pool) > 1 and current:
                others = [f for f in pool if f != current]
                if others:
                    pool = others
            chosen = secrets.choice(pool)

            port_to_slot = state["port_to_slot"]
            use_docker = state["use_docker"]
            with lock:
                state["active_ports"].discard(port)
                state["activation_cancelled_ports"].add(port)
                state["activation_state_by_port"][port] = "inactive"
                state["activation_error_by_port"].pop(port, None)
                slot = port_to_slot.get(port)
                if slot is not None:
                    loc_slot = slot.get("location_index")
                    if loc_slot is not None:
                        port_to_slot.pop(port_base + loc_slot, None)
                    teardown_slot(slot, use_docker)
                    slot["external_port"] = None
                    slot["location_index"] = None
                state["port_ovpn_assignment"][port] = chosen
            persist_assignments_snapshot(state)

            err = validate_location_assets(
                runtime_config,
                config_path,
                loc_idx,
                bool(state.get("use_docker")),
                chosen,
            )
            if err:
                with lock:
                    state["port_ovpn_assignment"].pop(port, None)
                    state["activation_cancelled_ports"].discard(port)
                persist_assignments_snapshot(state)
                self._send_error_body(err, 400)
                return

            with lock:
                state["activation_cancelled_ports"].discard(port)
                state["active_ports"].add(port)
                state["activation_state_by_port"][port] = "starting"
                state["activation_error_by_port"].pop(port, None)

            threading.Thread(
                target=_activate_port_async,
                args=(port, runtime_config, state),
                daemon=True,
            ).start()
            persist_assignments_snapshot(state)
            self._send_json(
                {
                    "ok": True,
                    "port": port,
                    "ovpn": chosen,
                    "locationIndex": loc_idx,
                    "activationState": "starting",
                }
            )

        def _handle_post_refresh_port(self, query: str) -> None:
            """Random-access only: tear down worker, keep assigned OVPN, start again."""
            params = urllib.parse.parse_qs(query)
            ports = params.get("port", [])
            if not ports:
                self._send_error_body("Missing port", 400)
                return
            try:
                port = int(ports[0])
            except ValueError:
                self._send_error_body("Invalid port", 400)
                return

            port_base = state["port_base"]
            num_ports = state.get("num_ports") or len(state["locations"])
            locations = state["locations"]
            loc_idx = port - port_base
            if loc_idx < 0 or loc_idx >= len(locations):
                self._send_error_body("Port out of location range", 400)
                return
            loc = locations[loc_idx]
            if not bool(loc.get("randomAccess")):
                self._send_error_body("Port is not a random-access slot", 403)
                return

            lock = state["lock"]
            with lock:
                assigned = (state["port_ovpn_assignment"].get(port) or "").strip()
            if not assigned:
                self._send_error_body("Select an OVPN file for this port before refresh", 400)
                return

            config_path = state["config_path"]
            runtime_config, load_err, load_status = load_disk_config_expanded(config_path)
            if load_err:
                self._send_error_body(load_err, load_status)
                return
            _enforce_default_proxy_auth(runtime_config)
            apply_openvpn_auth_env(runtime_config)

            err = validate_location_assets(
                runtime_config,
                config_path,
                loc_idx,
                bool(state.get("use_docker")),
                assigned,
            )
            if err:
                self._send_error_body(err, 400)
                return

            port_to_slot = state["port_to_slot"]
            use_docker = state["use_docker"]
            with lock:
                state["active_ports"].discard(port)
                state["activation_cancelled_ports"].add(port)
                state["activation_state_by_port"][port] = "inactive"
                state["activation_error_by_port"].pop(port, None)
                slot = port_to_slot.get(port)
                if slot is not None:
                    loc_slot = slot.get("location_index")
                    if loc_slot is not None:
                        port_to_slot.pop(port_base + loc_slot, None)
                    teardown_slot(slot, use_docker)
                    slot["external_port"] = None
                    slot["location_index"] = None

            with lock:
                state["activation_cancelled_ports"].discard(port)
                state["active_ports"].add(port)
                state["activation_state_by_port"][port] = "starting"
                state["activation_error_by_port"].pop(port, None)

            threading.Thread(
                target=_activate_port_async,
                args=(port, runtime_config, state),
                daemon=True,
            ).start()
            persist_assignments_snapshot(state)
            self._send_json(
                {
                    "ok": True,
                    "port": port,
                    "ovpn": assigned,
                    "locationIndex": loc_idx,
                    "activationState": "starting",
                }
            )

        def _handle_post_evict(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            ports = params.get("port", [])
            if not ports:
                self._send_error_body("Missing port", 400)
                return
            try:
                port = int(ports[0])
            except ValueError:
                self._send_error_body("Invalid port", 400)
                return
            lock = state["lock"]
            port_to_slot = state["port_to_slot"]
            slots = state["slots"]
            port_base = state["port_base"]
            use_docker = state["use_docker"]
            locations = state["locations"]
            slot = None
            with lock:
                slot = port_to_slot.get(port)
                if slot is not None:
                    loc = slot.get("location_index")
                    if loc is not None:
                        port_to_slot.pop(port_base + loc, None)
                    teardown_slot(slot, use_docker)
                    slot["external_port"] = None
                    slot["location_index"] = None
            if slot is not None:
                self._send_json({"ok": True})
            else:
                self._send_error_body("No active slot for port", 404)

    return GatewayControlHandler


def _run_control_server(
    control_port: int,
    gui_dir: Path,
    state: Dict[str, Any],
) -> None:
    handler_cls = _control_api_handler_factory(gui_dir, state)
    # Use threaded control server so long-running activation requests do not block
    # other API calls like /api/status and /api/ovpn-files.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", control_port), handler_cls)
    try:
        server.serve_forever()
    except Exception:
        pass
    finally:
        try:
            server.server_close()
        except Exception:
            pass


def main() -> int:
    _request_admin_rerun()
    parser = argparse.ArgumentParser(description="Dynamic proxy gateway (on-demand, idle shutdown).")
    parser.add_argument("--config", default=str(script_dir() / "openvpn-proxy-config.json"), help="Path to config JSON")
    args = parser.parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = script_dir() / args.config
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        return 1
    if config_path.is_dir():
        print(f"Config path is a directory, not a file: {config_path}. (If using Docker, ensure the host file exists so the bind mount is a file.)", file=sys.stderr)
        return 1

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    try:
        apply_location_spec(config)
    except ValueError as e:
        print(f"Invalid locationSpec: {e}", file=sys.stderr)
        return 1
    _enforce_default_proxy_auth(config)
    apply_openvpn_auth_env(config)

    locations = config.get("locations") or []
    _log(f"Config loaded: {len(locations)} locations from {config_path}")
    if not locations:
        _log(
            "No locations in config (add a locations[] array or a valid locationSpec). "
            "Control API will start so the dashboard can load; add locations and restart the gateway for proxy listeners."
        )

    use_docker = config.get("useDocker") is True or os.environ.get("USE_DOCKER", "").lower() in ("1", "true", "yes")
    docker_image = config.get("dockerImage") or os.environ.get("DOCKER_IMAGE", "portico-worker")
    docker_network = config.get("dockerNetwork") or os.environ.get("DOCKER_NETWORK", "proxynet")
    ovpn_volume_name = config.get("dockerOvpnVolume") or os.environ.get("DOCKER_OVPN_VOLUME", "ovpn_data")

    port_base = config.get("portBase", 50000)
    internal_port_base = config.get("internalPortBase", 51000)
    # Host-side proxy port for location 0 when Docker publishes e.g. 51000->50000 (UI / curl on host).
    _ppb_env = (os.environ.get("PUBLISHED_PROXY_PORT_BASE") or "").strip()
    published_proxy_port_base: Optional[int] = None
    if _ppb_env.isdigit():
        published_proxy_port_base = int(_ppb_env)
    else:
        _ppb_cfg = config.get("publishedPortBase")
        if isinstance(_ppb_cfg, int) and _ppb_cfg > 0:
            published_proxy_port_base = _ppb_cfg
    max_slots = config.get("maxSlots", 50)
    idle_timeout_minutes = config.get("idleTimeoutMinutes", 45)
    idle_timeout_seconds = idle_timeout_minutes * 60.0
    listen_host = config.get("proxyListenHost") or "0.0.0.0"
    if use_docker:
        listen_host = "0.0.0.0"  # must listen on all interfaces so Docker port publishing works

    num_ports = len(locations)
    if published_proxy_port_base is not None:
        if num_ports > 0:
            _log(
                f"Published proxy port base (host UI): {published_proxy_port_base} "
                f"(internal listeners {port_base}-{port_base + num_ports - 1})"
            )
        else:
            _log(
                f"Published proxy port base (host UI): {published_proxy_port_base} "
                "(no per-location listeners until config defines at least one location)"
            )
    # Windows select() supports at most 512 sockets
    if sys.platform == "win32" and num_ports > 512:
        _log("Warning: num_ports > 512 may fail on Windows (select limit). Consider reducing locations or port range.")
    # Linux/Unix: ensure ulimit -n is high enough for num_ports + control + connections (30+ ports often fails with default ulimit in Docker/systemd)
    if resource is not None and num_ports >= 30:
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            # need: num_ports (listeners) + 1 (control) + headroom for client/backend connections
            need = num_ports + 64
            if soft < need:
                _log(
                    "Warning: open file limit (ulimit -n = %d) may be too low for %d ports. "
                    "Raise with 'ulimit -n 4096' or set LimitNOFILE=4096 in systemd/Docker. Need at least ~%d." % (soft, num_ports, need)
                )
        except (OSError, ValueError):
            pass
    sockets_by_port: Dict[int, socket.socket] = {}
    for i in range(num_ports):
        port = port_base + i
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((listen_host, port))
            s.listen(64)
            s.setblocking(False)
            sockets_by_port[port] = s
            listening_sockets.append(s)
        except OSError as e:
            print(f"Failed to bind {listen_host}:{port}: {e}", file=sys.stderr)
            for ss in listening_sockets:
                try:
                    ss.close()
                except Exception:
                    pass
            return 1

    if num_ports > 0:
        _log(f"Listening on {listen_host}:{port_base}-{port_base + num_ports - 1} ({num_ports} ports)")
    else:
        _log("No proxy listener ports (0 locations); control API only.")
    _log(f"Backend: {'Docker' if use_docker else 'local'}; max_slots={max_slots} idle_timeout={idle_timeout_minutes}min")

    slots: List[Dict[str, Any]] = []
    port_to_slot: Dict[int, Dict[str, Any]] = {}
    active_ports: set = set()
    port_ovpn_assignment: Dict[int, str] = {}
    assignments_path = resolve_assignments_path(config_path)
    redis_url = _redis_url_from_env_or_config(config)
    redis_key = _redis_state_key()
    if redis_url:
        _log(f"Assignment store: Redis key={redis_key!r}")
    _loaded_assign, _loaded_active_ports = load_gateway_assignments_state(
        assignments_path,
        redis_url,
        redis_key,
        port_base,
        num_ports,
        config,
        config_path,
        use_docker,
    )
    port_ovpn_assignment.update(_loaded_assign)
    _log(
        f"Assignments ({assignments_path}): loaded {len(_loaded_assign)} OVPN pick(s), "
        f"{len(_loaded_active_ports)} persisted active port(s)"
    )

    activation_state_by_port: Dict[int, str] = {}
    activation_error_by_port: Dict[int, str] = {}
    activation_cancelled_ports: Set[int] = set()
    lock = threading.Lock()

    auto_activate_on_startup = config.get("autoActivateOnStartup", True)
    if not isinstance(auto_activate_on_startup, bool):
        auto_activate_on_startup = True

    gateway_state: Dict[str, Any] = {
        "slots": slots,
        "port_to_slot": port_to_slot,
        "active_ports": active_ports,
        "port_ovpn_assignment": port_ovpn_assignment,
        "activation_state_by_port": activation_state_by_port,
        "activation_error_by_port": activation_error_by_port,
        "activation_cancelled_ports": activation_cancelled_ports,
        "lock": lock,
        "config_path": config_path,
        "port_base": port_base,
        "max_slots": max_slots,
        "idle_timeout_minutes": idle_timeout_minutes,
        "use_docker": use_docker,
        "locations": locations,
        "listen_host": listen_host,
        "control_port": int(config.get("controlPort", CONTROL_PORT_DEFAULT) or 0),
        "num_ports": num_ports,
        "proxy_username": (config.get("proxyUsername") or "").strip(),
        "proxy_password": config.get("proxyPassword") or "",
        "published_port_base": published_proxy_port_base,
        "assignments_path": assignments_path,
        "redis_url": redis_url,
        "redis_state_key": redis_key,
    }

    idle_thread = threading.Thread(
        target=idle_eviction_loop,
        args=(gateway_state, idle_timeout_seconds, use_docker, port_base),
        daemon=True,
    )
    idle_thread.start()

    if auto_activate_on_startup and _loaded_active_ports:
        _enforce_default_proxy_auth(config)
        _log(
            f"autoActivateOnStartup: bringing up {len(_loaded_active_ports)} persisted listener port(s)"
        )
        for port in _loaded_active_ports:
            loc_idx = port - port_base
            if loc_idx < 0 or loc_idx >= len(locations):
                continue
            assigned = (port_ovpn_assignment.get(port) or "").strip()
            if not assigned:
                _log(f"Auto-activate skip port {port}: no OVPN assigned")
                continue
            err = validate_location_assets(
                config,
                config_path,
                loc_idx,
                use_docker,
                assigned,
            )
            if err:
                _log(f"Auto-activate skip port {port}: {err}")
                continue
            with lock:
                cur = activation_state_by_port.get(port, "inactive")
                if cur in ("active", "starting"):
                    continue
                activation_cancelled_ports.discard(port)
                active_ports.add(port)
                activation_state_by_port[port] = "starting"
                activation_error_by_port.pop(port, None)
            threading.Thread(
                target=_activate_port_async,
                args=(port, config, gateway_state),
                daemon=True,
            ).start()
        persist_assignments_snapshot(gateway_state)

    control_port = int(gateway_state["control_port"] or 0)
    if control_port > 0:
        gui_dir = script_dir() / "gui"
        control_thread = threading.Thread(
            target=_run_control_server,
            args=(control_port, gui_dir, gateway_state),
            daemon=True,
        )
        control_thread.start()
        _log(f"Control GUI: http://0.0.0.0:{control_port}")

    server_sockets = list(sockets_by_port.values())
    global shutdown_flag

    def _on_shutdown_signal(signum: int, frame: Any) -> None:
        global shutdown_flag
        shutdown_flag = True
        _log(f"Signal {signum} received, shutting down...")

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_shutdown_signal)
    signal.signal(signal.SIGINT, _on_shutdown_signal)

    try:
        while True:
            r, _, _ = select.select(server_sockets, [], [], 1)
            if shutdown_flag:
                break
            for sock in r:
                try:
                    client_sock, _ = sock.accept()
                except OSError:
                    continue
                port = port_base
                for p, s in sockets_by_port.items():
                    if s is sock:
                        port = p
                        break
                t = threading.Thread(
                    target=handle_connection,
                    args=(
                        client_sock,
                        port,
                        config,
                        config_path,
                        port_base,
                        internal_port_base,
                        max_slots,
                        slots,
                        port_to_slot,
                        active_ports,
                        port_ovpn_assignment,
                        activation_state_by_port,
                        lock,
                        use_docker,
                        docker_image,
                        docker_network,
                        ovpn_volume_name,
                    ),
                    daemon=True,
                )
                t.start()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_flag = True
        for s in listening_sockets:
            try:
                s.close()
            except Exception:
                pass
        with lock:
            for slot in slots:
                teardown_slot(slot, use_docker)
        if use_docker:
            try:
                from backend_docker import remove_all_dynamic_worker_containers

                remove_all_dynamic_worker_containers()
            except Exception as e:
                _log(f"Dynamic worker sweep on shutdown: {e}")
        print("Gateway stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
