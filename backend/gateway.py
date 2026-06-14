#!/usr/bin/env python3
"""
Portico — dynamic VPN proxy gateway: listens on one port per location (e.g. 50000, 50001, …),
runs at most maxSlots VPN+proxy pairs at a time, starts a proxy on-demand when a
client connects (holding the connection until ready), and shuts down proxies idle
for idleTimeoutMinutes (no proxy traffic; timer resets when bytes flow). Per listener port: HTTP or SOCKS5 proxy (one scheme per port).
"""
from pathlib import Path
# Allow importing openvpn_proxy_runner when run from project root
_sys_path = Path(__file__).resolve().parent
if str(_sys_path) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_sys_path))

import argparse
import base64
import errno
from email.parser import BytesParser
from email.policy import default as email_policy
import http.server
import json
import os
import secrets
import re
import shutil
import select
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
import tempfile
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
from openvpn_proxy_runner import resolve_ovpn_path, start_one_location, start_one_upstream_proxy
from provider_auth import load_provider_auth
from upstream_proxy import (
    UpstreamProxyError,
    import_proxy_lines,
    load_catalog,
    normalize_profile,
    public_profile,
    resolve_catalog_path,
    save_catalog,
)
from storage import PorticoStore, database_url as storage_database_url, enabled as storage_enabled

BUFFER_SIZE = 65536  # 64 KB max buffer while waiting for backend
BACKEND_READY_TIMEOUT = 90  # seconds to wait for proxy (cap so client can retry if VPN is slow)
BACKEND_POLL_INTERVAL = 0.2  # check backend readiness 5x per second
BACKEND_CONNECT_TIMEOUT = 0.3  # socket timeout when probing backend (fail fast)
IDLE_CHECK_INTERVAL = 60  # seconds between idle eviction passes
INITIAL_READ_SELECT_TIMEOUT = 0.01  # 10ms: proceed almost instantly after first chunk
INITIAL_READ_DEADLINE = 0.5  # max seconds to wait for first byte (avoids long stall per connection)
PORTS_PER_LOCATION = 1  # One client listener per location (pproxy: http or socks5 on worker :8080)
BACKEND_HTTP_PORT = 8080
EXTEND_PORT_IDLE_SECONDS = 30 * 60  # user extend: add this much idle budget (monotonic last_activity)
DB_STORE: Optional[PorticoStore] = None

# Public WAN IPv4 for dashboard proxy URLs when clientProxyHost is empty and listeners bind all interfaces.
_AUTO_WAN_IP_STATE: Dict[str, Any] = {"ip": None, "valid_until": 0.0, "cooldown_until": 0.0}
_AUTO_WAN_IP_LOCK = threading.Lock()
_AUTO_WAN_IP_TTL_SEC = 600.0
_AUTO_WAN_IP_FAIL_COOLDOWN_SEC = 45.0
_EGRESS_PUBLIC_IP_TTL_SEC = 300.0
_EGRESS_PUBLIC_IP_FAIL_COOLDOWN_SEC = 30.0


def _is_plain_ipv4(s: str) -> bool:
    parts = s.strip().split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _fetch_public_wan_ipv4_once() -> Optional[str]:
    import urllib.error
    import urllib.request as urllib_request

    ua = "Portico-Proxy-Gateway/1.0"

    def try_text(url: str) -> Optional[str]:
        try:
            req = urllib_request.Request(url, headers={"User-Agent": ua})
            with urllib_request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
            line = raw.splitlines()[0].strip() if raw else ""
            return line if _is_plain_ipv4(line) else None
        except (urllib.error.URLError, OSError, ValueError, UnicodeError):
            return None

    def try_ipify() -> Optional[str]:
        try:
            req = urllib_request.Request(
                "https://api.ipify.org?format=json",
                headers={"User-Agent": ua},
            )
            with urllib_request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
            data = json.loads(raw)
            ip = str(data.get("ip", "")).strip()
            return ip if _is_plain_ipv4(ip) else None
        except (urllib.error.URLError, OSError, ValueError, UnicodeError, json.JSONDecodeError, TypeError):
            return None

    for ip in (try_text("https://ifconfig.me/ip"), try_ipify(), try_text("https://icanhazip.com/")):
        if ip:
            return ip
    return None


def get_cached_public_wan_ipv4() -> Optional[str]:
    """Best-effort egress IPv4; cached with TTL and failure cooldown (no dependency beyond stdlib)."""
    now = time.monotonic()
    with _AUTO_WAN_IP_LOCK:
        ip = _AUTO_WAN_IP_STATE.get("ip")
        if ip and now < float(_AUTO_WAN_IP_STATE.get("valid_until") or 0.0):
            return str(ip)
        if now < float(_AUTO_WAN_IP_STATE.get("cooldown_until") or 0.0):
            return str(ip) if ip else None
    fetched = _fetch_public_wan_ipv4_once()
    now2 = time.monotonic()
    with _AUTO_WAN_IP_LOCK:
        if fetched:
            _AUTO_WAN_IP_STATE["ip"] = fetched
            _AUTO_WAN_IP_STATE["valid_until"] = now2 + _AUTO_WAN_IP_TTL_SEC
            _AUTO_WAN_IP_STATE["cooldown_until"] = 0.0
            return fetched
        _AUTO_WAN_IP_STATE["cooldown_until"] = now2 + _AUTO_WAN_IP_FAIL_COOLDOWN_SEC
        cur = _AUTO_WAN_IP_STATE.get("ip")
        return str(cur) if cur else None


def resolve_client_proxy_host(
    cfg_client: str,
    listen_host: str,
    auto_detect_wan: bool,
) -> Dict[str, str]:
    """Resolve the host clients should use in copied proxy strings."""
    explicit = (cfg_client or "").strip()
    if explicit:
        return {"host": explicit, "source": "config", "publicWanIp": ""}
    listen_h = (listen_host or "127.0.0.1").strip() or "127.0.0.1"
    if listen_h in ("0.0.0.0", "::", "[::]"):
        wan = get_cached_public_wan_ipv4() if auto_detect_wan else None
        if wan:
            return {"host": wan, "source": "auto-public-ip", "publicWanIp": wan}
        return {"host": "127.0.0.1", "source": "fallback-localhost", "publicWanIp": ""}
    return {"host": listen_h, "source": "listen-host", "publicWanIp": ""}


def auth_route_copy_host_payload(cfg_client: str, local_auth_routing: bool) -> Dict[str, Any]:
    """Describe how auth-route copy strings should choose their connect host."""
    explicit = (cfg_client or "").strip()
    if explicit:
        return {"copyHost": explicit, "copyHostSource": "config", "copyHostMode": "configured"}
    if local_auth_routing:
        return {"copyHost": "", "copyHostSource": "browser-local", "copyHostMode": "local"}
    return {"copyHost": "", "copyHostSource": "server-status", "copyHostMode": "server"}


def _parse_ipify_body(body: str) -> str:
    match = re.search(r'"ip"\s*:\s*"([^"]+)"', body or "")
    ip = match.group(1).strip() if match else (body or "").strip()
    return ip if _is_plain_ipv4(ip) else ""


def fetch_public_ip_via_proxy(
    proxy_host: str,
    proxy_port: int,
    proxy_type: str,
    username: str = "",
    password: str = "",
    timeout: float = 12.0,
) -> str:
    """Return the public IP seen after routing through a running proxy backend."""
    ptype = "socks5" if proxy_type == "socks5" else "http"
    if ptype == "http":
        import urllib.request as urllib_request

        auth = ""
        if username or password:
            user_enc = urllib.parse.quote(username or "", safe="")
            pass_enc = urllib.parse.quote(password or "", safe="")
            auth = f"{user_enc}:{pass_enc}@"
        proxy_url = f"http://{auth}{proxy_host}:{proxy_port}"
        proxy_handler = urllib_request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        opener = urllib_request.build_opener(proxy_handler)
        req = urllib_request.Request(
            "https://api.ipify.org?format=json",
            headers={"User-Agent": "Portico-Proxy-Gateway/1.0"},
        )
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        ip = _parse_ipify_body(body)
        if not ip:
            raise RuntimeError("ipify response did not contain a plain IPv4 address")
        return ip

    try:
        import socks  # type: ignore
    except ImportError as e:
        raise RuntimeError("SOCKS5 public IP check requires PySocks") from e
    s = socks.socksocket()
    s.set_proxy(
        socks.SOCKS5,
        proxy_host,
        proxy_port,
        rdns=False,
        username=username or None,
        password=password or None,
    )
    s.settimeout(timeout)
    try:
        s.connect(("api.ipify.org", 443))
        ctx = __import__("ssl").create_default_context()
        tls = ctx.wrap_socket(s, server_hostname="api.ipify.org")
        try:
            tls.sendall(
                b"GET /?format=json HTTP/1.1\r\n"
                b"Host: api.ipify.org\r\n"
                b"User-Agent: Portico-Proxy-Gateway/1.0\r\n"
                b"Connection: close\r\n\r\n"
            )
            chunks: List[bytes] = []
            while True:
                chunk = tls.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            tls.close()
    finally:
        try:
            s.close()
        except Exception:
            pass
    raw = b"".join(chunks).decode("utf-8", errors="replace")
    _hdr, _sep, body = raw.partition("\r\n\r\n")
    ip = _parse_ipify_body(body)
    if not ip:
        raise RuntimeError("ipify response did not contain a plain IPv4 address")
    return ip


listening_sockets: List[socket.socket] = []
shutdown_flag = False
CONTROL_PORT_DEFAULT = 49999
LOG_BUFFER_MAX = 1000
log_buffer: List[str] = []
DEFAULT_PROXY_USERNAME = "huzaifa"
DEFAULT_PROXY_PASSWORD = "huzaifa"
AUTH_HTTP_PORT_DEFAULT = 58080
AUTH_SOCKS_PORT_DEFAULT = 58081
AUTH_ROUTE_BACKEND_BASE = 60000
ALLOWED_ASSET_EXTENSIONS = {
    ".ovpn",
    ".crt",
    ".key",
    ".pem",
    ".p12",
    ".auth",
    ".txt",
}
OVPN_UPLOAD_MAX_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_FILE_NAME_LENGTH = 180


class OvpnUploadError(ValueError):
    """Raised when an OVPN upload batch is invalid or cannot be committed."""


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    line = f"[{ts}] [Gateway] {msg}"
    print(line, flush=True, file=sys.stderr)
    log_buffer.append(line)
    while len(log_buffer) > LOG_BUFFER_MAX:
        log_buffer.pop(0)


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def _cfg_int(val: Any, default: int) -> int:
    """Coerce JSON config values to int; bad or missing types must not crash the gateway."""
    try:
        if val is None:
            return default
        return int(val)
    except (TypeError, ValueError):
        return default


def _optional_env_positive_port(name: str) -> Optional[int]:
    raw = (os.environ.get(name) or "").strip()
    if not raw.isdigit():
        return None
    v = int(raw)
    if 1 <= v <= 65535:
        return v
    return None


def compute_docker_publish_alignment(
    port_base: int,
    num_ports: int,
    published_proxy_port_base: Optional[int],
) -> Dict[str, Any]:
    """
    Read compose-aligned env (DOCKER_PROXY_*); detect misalignment vs locations and portBase.
    Populates gateway_state and /api/status for UI hints.
    """
    h_first = _optional_env_positive_port("DOCKER_PROXY_HOST_PORT_FIRST")
    h_last = _optional_env_positive_port("DOCKER_PROXY_HOST_PORT_LAST")
    c_first = _optional_env_positive_port("DOCKER_PROXY_CONTAINER_PORT_FIRST")
    c_last = _optional_env_positive_port("DOCKER_PROXY_CONTAINER_PORT_LAST")

    reasons: List[str] = []
    host_span: Optional[int] = None
    container_span: Optional[int] = None

    if h_first is not None and h_last is not None:
        if h_first > h_last:
            reasons.append("DOCKER_PROXY_HOST_PORT_FIRST is greater than DOCKER_PROXY_HOST_PORT_LAST")
        else:
            host_span = h_last - h_first + 1
            if num_ports > host_span:
                reasons.append(
                    f"location count ({num_ports}) exceeds published host port span ({host_span}; {h_first}-{h_last})"
                )

    if c_first is not None and c_last is not None:
        if c_first > c_last:
            reasons.append(
                "DOCKER_PROXY_CONTAINER_PORT_FIRST is greater than DOCKER_PROXY_CONTAINER_PORT_LAST"
            )
        else:
            container_span = c_last - c_first + 1

    if host_span is not None and container_span is not None and host_span != container_span:
        reasons.append(
            f"host publish span ({host_span}) does not match container publish span ({container_span})"
        )

    if c_first is not None and num_ports > 0 and port_base != c_first:
        reasons.append(
            f"openvpn-proxy-config portBase ({port_base}) must equal DOCKER_PROXY_CONTAINER_PORT_FIRST ({c_first})"
        )

    if c_last is not None and num_ports > 0:
        port_max_cfg = port_base + num_ports - 1
        if port_max_cfg > c_last:
            reasons.append(
                f"last listener port ({port_max_cfg}) exceeds DOCKER_PROXY_CONTAINER_PORT_LAST ({c_last})"
            )

    if published_proxy_port_base is not None and h_first is not None and published_proxy_port_base != h_first:
        reasons.append(
            f"PUBLISHED_PROXY_PORT_BASE ({published_proxy_port_base}) should match "
            f"DOCKER_PROXY_HOST_PORT_FIRST ({h_first}) for linear Docker port mapping"
        )

    if published_proxy_port_base is not None and h_last is not None and num_ports > 0:
        implied_last = published_proxy_port_base + num_ports - 1
        if implied_last > h_last:
            reasons.append(
                f"implicit host ports through {implied_last} exceed DOCKER_PROXY_HOST_PORT_LAST ({h_last})"
            )

    hint = "; ".join(reasons) if reasons else ""
    return {
        "docker_published_host_port_first": h_first,
        "docker_published_host_port_last": h_last,
        "docker_published_port_span": host_span,
        "docker_published_container_port_first": c_first,
        "docker_published_container_port_last": c_last,
        "docker_published_container_port_span": container_span,
        "publish_mismatch": bool(reasons),
        "publish_mismatch_hint": hint,
    }


def _docker_container_publish_slot_count() -> Optional[int]:
    c_first = _optional_env_positive_port("DOCKER_PROXY_CONTAINER_PORT_FIRST")
    c_last = _optional_env_positive_port("DOCKER_PROXY_CONTAINER_PORT_LAST")
    if c_first is None or c_last is None or c_first > c_last:
        return None
    return c_last - c_first + 1


def _normalize_locations_to_slot_count(
    raw_locations: List[Dict[str, Any]],
    target_slot_count: int,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return exactly target_slot_count location dicts; pad or trim."""
    if target_slot_count < 1:
        return []
    raw = [dict(loc) for loc in raw_locations]
    if len(raw) >= target_slot_count:
        return raw[:target_slot_count]

    spec = config.get("locationSpec") if isinstance(config.get("locationSpec"), dict) else None
    default_ovpn = ""
    if spec:
        default_ovpn = (spec.get("defaultOvpn") or "").strip()
    if not default_ovpn and raw:
        default_ovpn = (str(raw[0].get("ovpn") or "")).strip()
    prefix = "slot"
    if spec:
        prefix = (str(spec.get("labelPrefix") or "slot")).strip() or "slot"

    template_user = (str(raw[0].get("username") or "")).strip() if raw else (str(config.get("username") or "")).strip()
    template_pass: Any = ""
    if raw:
        template_pass = raw[0].get("password") or ""
    if template_pass is None or template_pass == "":
        template_pass = config.get("password") or ""

    out = list(raw)
    i = len(out)
    while len(out) < target_slot_count:
        out.append(
            {
                "label": f"{prefix}-{i}",
                "ovpn": default_ovpn,
                "username": template_user,
                "password": template_pass,
                "randomAccess": False,
            }
        )
        i += 1
    return out


def apply_docker_published_listener_slots(
    locations_raw: List[Dict[str, Any]],
    config: Dict[str, Any],
    use_docker: bool,
) -> List[Dict[str, Any]]:
    """
    In Docker mode, when DOCKER_PROXY_CONTAINER_PORT_FIRST/LAST is set, use exactly that many
    TCP listeners: trim extra JSON rows or pad with synthetic slots (same defaults as locationSpec).
    """
    if not use_docker:
        return list(locations_raw)
    if not locations_raw:
        return []
    span = _docker_container_publish_slot_count()
    if span is None:
        return list(locations_raw)
    n_raw = len(locations_raw)
    if n_raw > span:
        _log(
            f"Docker publishes {span} container port(s); "
            f"using the first {span} location row(s), ignoring {n_raw - span} extra config row(s)."
        )
        return _normalize_locations_to_slot_count(locations_raw, span, config)
    if n_raw < span:
        _log(
            f"Docker publishes {span} container port(s); "
            f"padding from {n_raw} config row(s) to {span} listener(s) "
            f"(synthetic rows use locationSpec.defaultOvpn or the first location's OVPN)."
        )
        return _normalize_locations_to_slot_count(locations_raw, span, config)
    return list(locations_raw)


def merge_expanded_locations_from_disk(runtime_config: Dict[str, Any], use_docker: bool) -> Dict[str, Any]:
    """After load_disk_config_expanded: align locations[] with Docker publish span when applicable."""
    out = dict(runtime_config)
    raw = list(runtime_config.get("locations") or [])
    out["locations"] = apply_docker_published_listener_slots(raw, runtime_config, use_docker)
    return out


def _enforce_default_proxy_auth(config: Dict[str, Any]) -> None:
    user = (config.get("proxyUsername") or "").strip()
    password = config.get("proxyPassword") or ""
    if not user or not password:
        config["proxyUsername"] = DEFAULT_PROXY_USERNAME
        config["proxyPassword"] = DEFAULT_PROXY_PASSWORD


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def is_auth_routing_enabled(config: Dict[str, Any]) -> bool:
    if _env_truthy("AUTH_ROUTING_ENABLED"):
        return True
    auth_cfg = config.get("authRouting")
    return isinstance(auth_cfg, dict) and bool(auth_cfg.get("enabled"))


def _auth_routing_dict(config: Dict[str, Any]) -> Dict[str, Any]:
    auth_cfg = config.get("authRouting")
    return auth_cfg if isinstance(auth_cfg, dict) else {}


def _auth_global_password(config: Dict[str, Any]) -> str:
    env_password = os.environ.get("PROXY_GLOBAL_PASSWORD")
    if env_password is not None and env_password != "":
        return env_password
    auth_cfg = _auth_routing_dict(config)
    cfg_password = auth_cfg.get("globalPassword")
    if cfg_password is not None and str(cfg_password) != "":
        return str(cfg_password)
    cfg_proxy_password = config.get("proxyPassword")
    if cfg_proxy_password is not None and str(cfg_proxy_password) != "":
        return str(cfg_proxy_password)
    return DEFAULT_PROXY_PASSWORD


def _auth_http_port(config: Dict[str, Any]) -> int:
    raw = (os.environ.get("AUTH_HTTP_PORT") or "").strip()
    if raw.isdigit():
        return max(1, min(65535, int(raw)))
    return max(1, min(65535, _cfg_int(_auth_routing_dict(config).get("httpPort"), AUTH_HTTP_PORT_DEFAULT)))


def _auth_socks_port(config: Dict[str, Any]) -> int:
    raw = (os.environ.get("AUTH_SOCKS_PORT") or "").strip()
    if raw.isdigit():
        return max(1, min(65535, int(raw)))
    return max(1, min(65535, _cfg_int(_auth_routing_dict(config).get("socksPort"), AUTH_SOCKS_PORT_DEFAULT)))


def _normalize_auth_route_egress(route: Dict[str, Any]) -> Dict[str, str]:
    egress = route.get("egress") if isinstance(route.get("egress"), dict) else {}
    egress_type = (str(egress.get("type") or route.get("egressType") or "")).strip().lower()
    ovpn = (str(egress.get("ovpn") or route.get("ovpn") or "")).strip()
    upstream_id = (
        str(egress.get("upstreamProxyId") or route.get("upstreamProxyId") or "")
    ).strip()
    if egress_type == "upstream" or upstream_id:
        return {"type": "upstream", "upstreamProxyId": upstream_id} if upstream_id else {"type": "none"}
    if egress_type == "ovpn" or ovpn:
        return {"type": "ovpn", "ovpn": ovpn} if ovpn else {"type": "none"}
    return {"type": "none"}


def normalize_auth_routes(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    auth_cfg = _auth_routing_dict(config)
    raw_routes = auth_cfg.get("routes") if isinstance(auth_cfg.get("routes"), list) else []
    routes: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for idx, raw in enumerate(raw_routes):
        if not isinstance(raw, dict):
            continue
        username = (str(raw.get("username") or "")).strip()
        if not username or username in seen:
            continue
        seen.add(username)
        label = (str(raw.get("label") or username)).strip() or username
        external_id = (str(raw.get("externalId") or raw.get("external_id") or "")).strip()
        proxy_type = "socks5" if (str(raw.get("proxyType") or raw.get("proxy_type") or "")).strip().lower() == "socks5" else "http"
        try:
            rotation_minutes = int(raw.get("rotationIntervalMinutes") or raw.get("rotation_interval_minutes") or 0)
        except (TypeError, ValueError):
            rotation_minutes = 0
        rotation_minutes = max(0, min(_ROTATION_INTERVAL_MAX_MINUTES, rotation_minutes))
        rotation_country_raw = (str(raw.get("rotationCountry") or raw.get("rotation_country") or "")).strip()
        rotation_country = ""
        if rotation_minutes > 0 and rotation_country_raw:
            norm_country = normalize_randomize_country(rotation_country_raw)
            if norm_country != "random":
                rotation_country = norm_country
        try:
            rotation_last_run = float(raw.get("rotationLastRun") or raw.get("rotation_last_run") or 0.0)
        except (TypeError, ValueError):
            rotation_last_run = 0.0
        enabled = raw.get("enabled")
        if enabled is None:
            enabled = True
        egress = _normalize_auth_route_egress(raw)
        if egress.get("type") != "ovpn":
            rotation_minutes = 0
            rotation_country = ""
            rotation_last_run = 0.0
        routes.append(
            {
                "index": len(routes),
                "username": username,
                "label": label,
                "externalId": external_id,
                "proxyType": proxy_type,
                "rotationIntervalMinutes": rotation_minutes,
                "rotationCountry": rotation_country,
                "rotationLastRun": rotation_last_run if rotation_minutes > 0 else 0.0,
                "enabled": bool(enabled),
                "egress": egress,
            }
        )
    return routes


def _auth_username_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return slug[:32].strip("_") or "proxy"


def _auth_username_seed(payload: Dict[str, Any]) -> str:
    explicit = (str(payload.get("username") or "")).strip()
    if explicit:
        return explicit
    for key in ("externalId", "external_id", "label"):
        raw = (str(payload.get(key) or "")).strip()
        if raw:
            return raw
    egress = _normalize_auth_route_egress(payload)
    ovpn = (egress.get("ovpn") or "").strip()
    if ovpn:
        pieces = [p for p in re.split(r"[\\/]+", ovpn) if p]
        provider = pieces[0] if len(pieces) > 1 else ""
        basename = Path(pieces[-1]).stem if pieces else ovpn
        return "_".join([p for p in (provider, basename) if p])
    upstream_id = (egress.get("upstreamProxyId") or "").strip()
    if upstream_id:
        return upstream_id
    return "proxy"


def _auth_route_unique_username(payload: Dict[str, Any], routes: Iterable[Dict[str, Any]]) -> str:
    existing = {
        (str(route.get("username") or "")).strip()
        for route in routes
        if (str(route.get("username") or "")).strip()
    }
    base = _auth_username_slug(_auth_username_seed(payload))
    requested = (str(payload.get("username") or "")).strip()
    requested_slug = _auth_username_slug(requested) if requested else ""
    if requested and requested == requested_slug and requested not in existing:
        return requested
    if requested:
        base = requested_slug
    for _ in range(32):
        candidate = f"{base}_{secrets.token_hex(2)}"
        if candidate not in existing:
            return candidate
    return f"{base}_{secrets.token_hex(4)}"


def _auth_route_backend_key(route_index: int, scheme: str) -> int:
    offset = 1 if (scheme or "").lower() == "socks5" else 0
    return AUTH_ROUTE_BACKEND_BASE + (route_index * 2) + offset


def _auth_route_container_name(backend_key: int) -> str:
    return f"proxy-{backend_key}"


def _auth_route_protocol_key(username: str, scheme: str) -> str:
    return f"{username}:{'socks5' if scheme == 'socks5' else 'http'}"


def _auth_route_docker_container_state(backend_key: int) -> Dict[str, Any]:
    try:
        from backend_docker import inspect_worker_container

        return inspect_worker_container(_auth_route_container_name(backend_key))
    except Exception:
        return {
            "exists": False,
            "running": False,
            "name": _auth_route_container_name(backend_key),
            "status": "",
            "id": "",
        }


def _remove_auth_route_docker_container(backend_key: int) -> bool:
    try:
        from backend_docker import remove_worker_container_by_name

        return remove_worker_container_by_name(_auth_route_container_name(backend_key))
    except Exception:
        return False


def _make_auth_route_slot(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for candidate in state["slots"]:
        if candidate.get("external_port") is None:
            return candidate
    if len([s for s in state["slots"] if s.get("external_port") is not None]) >= state["max_slots"]:
        used = [s for s in state["slots"] if s.get("external_port") is not None]
        if not used:
            return None
        oldest = min(used, key=lambda s: s["last_activity"])
        old_key = oldest.get("external_port")
        if old_key is not None:
            state["port_to_slot"].pop(old_key, None)
        teardown_slot(oldest, bool(state.get("use_docker")))
        return oldest
    if len(state["slots"]) >= state["max_slots"]:
        return None
    slot = {
        "internal_port": int(state.get("internal_port_base", 51000)) + len(state["slots"]),
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
        "proxy_type": None,
        "egress_type": None,
        "route_username": None,
    }
    state["slots"].append(slot)
    return slot


def _refresh_auth_route_egress_ip(state: Dict[str, Any], username: str, scheme: str, slot: Dict[str, Any]) -> None:
    key = _auth_route_protocol_key(username, scheme)
    host = str(slot.get("backend_host") or "").strip()
    try:
        port = int(slot.get("backend_port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not host or port <= 0:
        return
    try:
        ip = fetch_public_ip_via_proxy(host, port, "socks5" if scheme == "socks5" else "http")
        payload = {"ip": ip, "checkedAt": time.time(), "error": "", "validUntil": time.time() + _EGRESS_PUBLIC_IP_TTL_SEC}
    except Exception as e:
        payload = {
            "ip": "",
            "checkedAt": time.time(),
            "error": str(e),
            "validUntil": time.time() + _EGRESS_PUBLIC_IP_FAIL_COOLDOWN_SEC,
        }
    with state["lock"]:
        state.setdefault("auth_route_egress_ip", {})[key] = payload
        state.setdefault("auth_route_egress_ip_refreshing", set()).discard(key)


def _maybe_start_auth_route_egress_ip_refresh(
    state: Dict[str, Any],
    username: str,
    scheme: str,
    slot: Optional[Dict[str, Any]],
    force: bool = False,
) -> None:
    if not slot:
        return
    key = _auth_route_protocol_key(username, scheme)
    now = time.time()
    with state["lock"]:
        cache = dict((state.get("auth_route_egress_ip") or {}).get(key) or {})
        refreshing = state.setdefault("auth_route_egress_ip_refreshing", set())
        if key in refreshing:
            return
        if not force and now < float(cache.get("validUntil") or 0.0):
            return
        refreshing.add(key)
    threading.Thread(
        target=_refresh_auth_route_egress_ip,
        args=(state, username, scheme, dict(slot)),
        daemon=True,
    ).start()


def _auth_route_location_config(routes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    locations: List[Dict[str, Any]] = []
    for route in routes:
        egress = route.get("egress") or {}
        locations.append(
            {
                "label": route.get("label") or route.get("username") or "",
                "ovpn": egress.get("ovpn", "") if egress.get("type") == "ovpn" else "",
            }
        )
    return locations


def _auth_route_by_username(
    routes: Iterable[Dict[str, Any]],
    username: str,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    target = (username or "").strip()
    for idx, route in enumerate(routes):
        if (route.get("username") or "").strip() == target:
            return idx, route
    return None


def _route_password_matches(expected: str, supplied: str) -> bool:
    return secrets.compare_digest(str(expected or ""), str(supplied or ""))


def _persist_auth_routes_config(config_path: Path, state: Dict[str, Any], routes: List[Dict[str, Any]]) -> Optional[str]:
    store = state.get("db_store")
    if store is not None:
        try:
            store.save_auth_routes(routes)
            cfg = dict(state.get("auth_runtime_config") or {})
            auth_cfg = dict(_auth_routing_dict(cfg))
            auth_cfg["enabled"] = True
            auth_cfg["httpPort"] = int(state.get("auth_http_port") or AUTH_HTTP_PORT_DEFAULT)
            auth_cfg["socksPort"] = int(state.get("auth_socks_port") or AUTH_SOCKS_PORT_DEFAULT)
            auth_cfg["routes"] = routes
            cfg["authRouting"] = auth_cfg
            store.save_config(_prepare_config_for_disk(cfg))
            return None
        except Exception as e:
            return str(e)
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return f"Could not read config: {e}"
    if not isinstance(cfg, dict):
        return "Config root must be an object"
    auth_cfg = cfg.get("authRouting")
    if not isinstance(auth_cfg, dict):
        auth_cfg = {}
    auth_cfg["enabled"] = True
    auth_cfg["httpPort"] = int(state.get("auth_http_port") or AUTH_HTTP_PORT_DEFAULT)
    auth_cfg["socksPort"] = int(state.get("auth_socks_port") or AUTH_SOCKS_PORT_DEFAULT)
    auth_cfg["routes"] = [
        {
            "username": route.get("username") or "",
            "label": route.get("label") or route.get("username") or "",
            "externalId": route.get("externalId") or route.get("external_id") or "",
            "proxyType": "socks5" if route.get("proxyType") == "socks5" else "http",
            "rotationIntervalMinutes": int(route.get("rotationIntervalMinutes") or 0),
            "rotationCountry": route.get("rotationCountry") or "",
            "rotationLastRun": float(route.get("rotationLastRun") or 0.0),
            "enabled": bool(route.get("enabled", True)),
            "egress": dict(route.get("egress") or {"type": "none"}),
        }
        for route in routes
    ]
    cfg["authRouting"] = auth_cfg
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        return str(e)
    return None


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


def attach_provider_credentials(config: Dict[str, Any]) -> None:
    global DB_STORE
    if DB_STORE is None:
        return
    try:
        config["_providerCredentials"] = DB_STORE.load_provider_credentials()
    except Exception as e:
        _log(f"Could not load provider credentials from Postgres: {e}")


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


def _resolve_provider_auth_root(config: Dict[str, Any], config_path: Path, use_docker: bool) -> Path:
    """Resolve the root folder where provider auth files live."""
    if use_docker:
        return _docker_ovpn_mount_path()
    base_dir = config_path.resolve().parent
    if config.get("ovpnRoot"):
        return (base_dir / str(config.get("ovpnRoot") or "")).resolve()
    return base_dir.resolve()


def _parse_auth_file_credentials(auth_path: Path) -> Tuple[str, str]:
    try:
        lines = auth_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", ""
    username = lines[0].strip() if len(lines) >= 1 else ""
    password = lines[1].strip() if len(lines) >= 2 else ""
    return username, password


def _collect_provider_auth_rows(
    config: Dict[str, Any],
    config_path: Path,
    use_docker: bool,
    provider_credentials: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str], int]:
    ovpn_root = _resolve_provider_auth_root(config, config_path, use_docker)
    if not ovpn_root.exists() or not ovpn_root.is_dir():
        return [], f"ovpnRoot does not exist or is not a directory: {ovpn_root}", 400

    provider_names: Set[str] = set()
    for name in (provider_credentials or {}).keys():
        if name == name.casefold():
            continue
        if _is_safe_provider_name(name):
            provider_names.add(name)
    for p in ovpn_root.rglob("*.ovpn"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(ovpn_root)
        except ValueError:
            continue
        if len(rel.parts) >= 2:
            provider_names.add(rel.parts[0])
    for child in ovpn_root.iterdir():
        if child.is_dir() and (child / "auth.txt").is_file():
            provider_names.add(child.name)

    rows: List[Dict[str, Any]] = []
    for provider in sorted(provider_names, key=lambda s: s.casefold()):
        provider_dir = ovpn_root / provider
        auth_path = provider_dir / "auth.txt"
        username = ""
        password = ""
        has_auth_file = auth_path.is_file()
        db_row = (provider_credentials or {}).get(provider) or (provider_credentials or {}).get(provider.casefold())
        if isinstance(db_row, dict):
            username = db_row.get("username") or ""
            password = db_row.get("password") or ""
        elif has_auth_file:
            username, password = _parse_auth_file_credentials(auth_path)
        rows.append(
            {
                "provider": provider,
                "authPath": str(auth_path.resolve()),
                "hasAuthFile": has_auth_file,
                "hasDbCredentials": bool(isinstance(db_row, dict) and username and password),
                "username": username,
                "password": password,
            }
        )

    return rows, None, 200


def _is_safe_provider_name(name: str) -> bool:
    s = (name or "").strip()
    if not s:
        return False
    if "/" in s or "\\" in s:
        return False
    if s in (".", ".."):
        return False
    if ":" in s or len(s) > 120:
        return False
    return not any(c in s for c in "\r\n\t\x00")


def _is_safe_upload_filename(name: str) -> bool:
    s = (name or "").strip()
    if not s or len(s) > MAX_UPLOAD_FILE_NAME_LENGTH:
        return False
    if "/" in s or "\\" in s or ":" in s:
        return False
    if s in (".", "..") or Path(s).is_absolute():
        return False
    if any(c in s for c in "\r\n\t\x00"):
        return False
    if len(Path(s).parts) != 1:
        return False
    return Path(s).suffix.lower() in ALLOWED_ASSET_EXTENSIONS


def _provider_file_summary(ovpn_root: Path) -> List[Dict[str, Any]]:
    if not ovpn_root.exists() or not ovpn_root.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for child in sorted((p for p in ovpn_root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        ovpn_count = 0
        asset_count = 0
        for p in child.iterdir():
            if not p.is_file():
                continue
            suffix = p.suffix.lower()
            if suffix == ".ovpn":
                ovpn_count += 1
            elif suffix in ALLOWED_ASSET_EXTENSIONS:
                asset_count += 1
        if ovpn_count or asset_count or (child / "auth.txt").is_file():
            rows.append(
                {
                    "provider": child.name,
                    "ovpnCount": ovpn_count,
                    "assetCount": asset_count,
                    "hasAuthFile": (child / "auth.txt").is_file(),
                }
            )
    return rows


def save_ovpn_upload_batch(
    ovpn_root: Path,
    provider: str,
    username: str,
    password: str,
    files: List[Dict[str, Any]],
    overwrite: bool = False,
    write_auth_file: bool = True,
) -> Dict[str, Any]:
    """
    Validate and commit a loose-file OVPN upload batch into one provider folder.

    files entries are {"filename": str, "data": bytes}. In legacy file mode the
    provider auth.txt is derived from username/password, so uploaded auth.txt is reserved.
    """
    provider_name = (provider or "").strip()
    if not _is_safe_provider_name(provider_name):
        raise OvpnUploadError("provider must be a safe single folder name")
    if any(c in str(username or "") for c in "\r\n\x00"):
        raise OvpnUploadError("username contains invalid characters")
    if any(c in str(password or "") for c in "\r\n\x00"):
        raise OvpnUploadError("password contains invalid characters")
    username = str(username or "").strip()
    password = str(password or "")
    if write_auth_file and (not username or not password):
        raise OvpnUploadError("username and password are required")
    if not files:
        raise OvpnUploadError("at least one upload file is required")
    root = ovpn_root.resolve()
    if not root.exists() or not root.is_dir():
        raise OvpnUploadError(f"ovpnRoot does not exist or is not a directory: {root}")

    normalized: List[Tuple[str, bytes]] = []
    seen_names: Set[str] = set()
    for item in files:
        filename = str(item.get("filename") or "").strip()
        data = item.get("data")
        if not isinstance(data, (bytes, bytearray)):
            raise OvpnUploadError(f"{filename or 'upload'} is not a file upload")
        if not _is_safe_upload_filename(filename):
            raise OvpnUploadError(f"unsafe or unsupported upload filename: {filename}")
        if filename.casefold() == "auth.txt":
            raise OvpnUploadError("auth.txt is managed from the upload username/password fields")
        if len(data) == 0:
            raise OvpnUploadError(f"upload file is empty: {filename}")
        key = filename.casefold()
        if key in seen_names:
            raise OvpnUploadError(f"duplicate filename in upload batch: {filename}")
        seen_names.add(key)
        normalized.append((filename, bytes(data)))

    provider_dir = (root / provider_name).resolve()
    if not _is_safe_under_root(provider_dir, root):
        raise OvpnUploadError("provider path escapes ovpnRoot")
    conflicts: List[str] = []
    for filename, _data in normalized:
        target = (provider_dir / filename).resolve()
        if not _is_safe_under_root(target, root):
            raise OvpnUploadError(f"upload path escapes ovpnRoot: {filename}")
        if target.exists() and not overwrite:
            conflicts.append(filename)
    if conflicts:
        raise OvpnUploadError(
            "file already exists: " + ", ".join(conflicts[:5]) + ("..." if len(conflicts) > 5 else "")
        )

    stage_dir = Path(tempfile.mkdtemp(prefix=f".{provider_name}.upload-", dir=str(root)))
    try:
        staged_files: List[Tuple[Path, Path, str]] = []
        for filename, data in normalized:
            stage_path = stage_dir / filename
            stage_path.write_bytes(data)
            staged_files.append((stage_path, (provider_dir / filename).resolve(), filename))
        staged_names = {filename.casefold() for _stage_path, _target_path, filename in staged_files}
        for stage_path, _target_path, filename in staged_files:
            if Path(filename).suffix.lower() != ".ovpn":
                continue
            try:
                refs = _extract_referenced_assets(stage_path)
            except OSError as exc:
                raise OvpnUploadError(f"Failed to read OVPN file {filename}: {exc}") from exc
            for ref in refs:
                ref_path = Path(ref)
                if ref_path.suffix.lower() not in ALLOWED_ASSET_EXTENSIONS:
                    raise OvpnUploadError(f"Referenced file extension not allowed in {filename}: {ref}")
                stage_ref = (stage_path.parent / ref).resolve()
                target_ref = (provider_dir / ref).resolve()
                if not _is_safe_under_root(stage_ref, stage_dir) or not _is_safe_under_root(target_ref, root):
                    raise OvpnUploadError(f"Referenced file escapes provider folder in {filename}: {ref}")
                if len(ref_path.parts) == 1 and ref_path.name.casefold() in staged_names:
                    continue
                if not target_ref.exists():
                    raise OvpnUploadError(f"Referenced OpenVPN asset missing for {filename}: {ref}")
        if write_auth_file:
            auth_stage = stage_dir / "auth.txt"
            auth_stage.write_text(f"{username}\n{password}\n", encoding="utf-8")
            staged_files.append((auth_stage, (provider_dir / "auth.txt").resolve(), "auth.txt"))

        provider_dir.mkdir(parents=True, exist_ok=True)
        for _stage_path, target_path, filename in staged_files:
            if filename != "auth.txt" and target_path.exists() and not overwrite:
                raise OvpnUploadError(f"file already exists: {filename}")
        for stage_path, target_path, _filename in staged_files:
            os.replace(stage_path, target_path)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EROFS or "read-only" in str(exc).lower():
            raise OvpnUploadError(
                "OVPN root is read-only. Ensure the gateway mounts ovpn_data as writable."
            ) from exc
        raise OvpnUploadError(str(exc)) from exc
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    uploaded_files = [name for name, _data in normalized]
    return {
        "ok": True,
        "provider": provider_name,
        "uploaded": len(uploaded_files),
        "ovpnUploaded": sum(1 for name in uploaded_files if Path(name).suffix.lower() == ".ovpn"),
        "files": uploaded_files,
        "authPath": str((provider_dir / "auth.txt").resolve()) if write_auth_file else "",
        "credentialsStoredIn": "auth.txt" if write_auth_file else ("postgres" if username and password else "none"),
    }


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
        payload["providers"] = _provider_file_summary(mount)
        if not path_exists or not is_dir:
            payload["hint"] = (
                f"OVPN mount missing or not a directory at {mount}. "
                "Ensure docker-compose mounts ovpn_data at /ovpn on the gateway (see README)."
            )
        elif len(files) == 0:
            payload["hint"] = (
                "No .ovpn files under the gateway mount. Upload OVPN files from the dashboard; "
                "they will be stored in the ovpn_data Docker volume at /ovpn."
            )
    else:
        base_dir = config_path.resolve().parent
        ovpn_root = (base_dir / config["ovpnRoot"]).resolve() if config.get("ovpnRoot") else base_dir.resolve()
        path_exists = ovpn_root.exists()
        is_dir = ovpn_root.is_dir() if path_exists else False
        payload["scanPath"] = str(ovpn_root)
        payload["pathExists"] = path_exists
        payload["isDirectory"] = is_dir
        payload["providers"] = _provider_file_summary(ovpn_root)
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
    global DB_STORE
    if DB_STORE is not None:
        try:
            cfg = DB_STORE.load_config()
        except Exception as e:
            return None, f"Could not read config from database: {e}", 500
    else:
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


ASSIGNMENTS_STATE_VERSION = 2
UPSTREAM_CATALOG_ENV = "UPSTREAM_PROXY_CATALOG_PATH"


def resolve_assignments_path(config_path: Path) -> Path:
    override = (os.environ.get("OPENVPN_PROXY_ASSIGNMENTS_PATH") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (config_path.parent / "openvpn-proxy-assignments.json").resolve()


def resolve_upstream_catalog_path(config_path: Path) -> Path:
    return resolve_catalog_path(config_path, os.environ.get(UPSTREAM_CATALOG_ENV) or "")


def _catalog_index(profiles: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(p.get("id")): dict(p) for p in profiles if (p.get("id") or "").strip()}


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
    launcher_ids: Optional[Dict[int, str]] = None,
    proxy_types: Optional[Dict[int, str]] = None,
    rotation_intervals: Optional[Dict[int, int]] = None,
    rotation_countries: Optional[Dict[int, str]] = None,
    rotation_last_run: Optional[Dict[int, float]] = None,
    egress_by_port: Optional[Dict[int, Dict[str, str]]] = None,
    upstream_refresh_intervals: Optional[Dict[int, int]] = None,
    upstream_refresh_last_run: Optional[Dict[int, float]] = None,
) -> Dict[str, Any]:
    ap_list = sorted(set(active_ports)) if active_ports is not None else []
    lid = launcher_ids or {}
    lid_out = {str(p): s for p, s in sorted(lid.items()) if (s or "").strip()}
    pt = proxy_types or {}
    # Persist only socks5 overrides; missing port implies http
    pt_out = {str(p): t for p, t in sorted(pt.items()) if t == "socks5"}
    ri = rotation_intervals or {}
    ri_out = {str(p): int(v) for p, v in sorted(ri.items()) if isinstance(v, int) and v > 0}
    rc = rotation_countries or {}
    rc_out = {str(p): str(v) for p, v in sorted(rc.items()) if (v or "").strip()}
    rl = rotation_last_run or {}
    # Only persist last_run for ports that actually have an active rotation interval.
    rl_out = {
        str(p): float(v)
        for p, v in sorted(rl.items())
        if isinstance(v, (int, float)) and v > 0 and ri.get(p, 0) > 0
    }
    egress_out: Dict[str, Dict[str, str]] = {}
    for port, egress in sorted((egress_by_port or {}).items()):
        egress_type = (egress.get("type") or "").strip().lower()
        if egress_type == "ovpn" and (egress.get("ovpn") or "").strip():
            egress_out[str(port)] = {"type": "ovpn", "ovpn": egress["ovpn"].strip()}
        elif egress_type == "upstream" and (egress.get("upstreamProxyId") or "").strip():
            egress_out[str(port)] = {
                "type": "upstream",
                "upstreamProxyId": egress["upstreamProxyId"].strip(),
            }
    uri = upstream_refresh_intervals or {}
    uri_out = {str(p): int(v) for p, v in sorted(uri.items()) if isinstance(v, int) and v > 0}
    urlr = upstream_refresh_last_run or {}
    urlr_out = {
        str(p): float(v)
        for p, v in sorted(urlr.items())
        if isinstance(v, (int, float)) and v > 0 and uri.get(p, 0) > 0
    }
    payload: Dict[str, Any] = {
        "version": ASSIGNMENTS_STATE_VERSION,
        "assignments": {str(p): name for p, name in sorted(assignments.items())},
        "activePorts": ap_list,
    }
    if egress_out:
        payload["egress"] = egress_out
    if lid_out:
        payload["launcherIds"] = lid_out
    if pt_out:
        payload["proxyTypes"] = pt_out
    if ri_out:
        payload["rotationIntervals"] = ri_out
    if rc_out:
        payload["rotationCountries"] = rc_out
    if rl_out:
        payload["rotationLastRun"] = rl_out
    if uri_out:
        payload["upstreamRefreshIntervals"] = uri_out
    if urlr_out:
        payload["upstreamRefreshLastRun"] = urlr_out
    return payload


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


def _egress_from_assignments(assignments: Dict[int, str]) -> Dict[int, Dict[str, str]]:
    return {p: {"type": "ovpn", "ovpn": name} for p, name in assignments.items() if name}


def _egress_ovpn_assignments(egress_by_port: Dict[int, Dict[str, str]]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for port, egress in egress_by_port.items():
        if (egress.get("type") or "").lower() == "ovpn" and (egress.get("ovpn") or "").strip():
            out[port] = egress["ovpn"].strip()
    return out


def _parse_egress_block(
    raw: Any,
    port_base: int,
    num_ports: int,
    allowed_ovpn: Set[str],
) -> Dict[int, Dict[str, str]]:
    out: Dict[int, Dict[str, str]] = {}
    if not isinstance(raw, dict) or num_ports <= 0:
        return out
    port_max = port_base + num_ports - 1
    for k, v in raw.items():
        try:
            port = int(str(k))
        except (TypeError, ValueError):
            continue
        if port < port_base or port > port_max or not isinstance(v, dict):
            continue
        egress_type = (str(v.get("type") or "")).strip().lower()
        if egress_type == "ovpn":
            ovpn = (str(v.get("ovpn") or "")).strip()
            parsed = _parse_assignments_block({str(port): ovpn}, port_base, num_ports, allowed_ovpn, relaxed=False)
            if not parsed and ovpn:
                parsed = _parse_assignments_block({str(port): ovpn}, port_base, num_ports, allowed_ovpn, relaxed=True)
            if parsed.get(port):
                out[port] = {"type": "ovpn", "ovpn": parsed[port]}
        elif egress_type == "upstream":
            profile_id = (str(v.get("upstreamProxyId") or "")).strip()
            if not profile_id or len(profile_id) > 128 or any(c in profile_id for c in "\r\n\t\x00"):
                continue
            out[port] = {"type": "upstream", "upstreamProxyId": profile_id}
    return out


def _parse_launcher_ids_block(
    raw_ids: Any,
    port_base: int,
    num_ports: int,
) -> Dict[int, str]:
    """Optional per-listener-port user IDs from assignments JSON."""
    out: Dict[int, str] = {}
    if not isinstance(raw_ids, dict) or num_ports <= 0:
        return out
    port_max = port_base + num_ports - 1
    for k, v in raw_ids.items():
        try:
            port = int(str(k))
        except (TypeError, ValueError):
            continue
        if port < port_base or port > port_max:
            continue
        s = (str(v) if v is not None else "").strip()
        if not s:
            continue
        if len(s) > 256:
            s = s[:256]
        if any(c in s for c in "\r\n\t\x00"):
            continue
        out[port] = s
    return out


def _parse_proxy_types_block(
    raw_types: Any,
    port_base: int,
    num_ports: int,
) -> Dict[int, str]:
    """Per-listener SOCKS5 overrides only (missing port => http)."""
    out: Dict[int, str] = {}
    if not isinstance(raw_types, dict) or num_ports <= 0:
        return out
    port_max = port_base + num_ports - 1
    for k, v in raw_types.items():
        try:
            port = int(str(k))
        except (TypeError, ValueError):
            continue
        if port < port_base or port > port_max:
            continue
        s = (str(v) if v is not None else "").strip().lower()
        if s == "socks5":
            out[port] = "socks5"
    return out


# Hard cap on rotation interval to keep persisted values sane (1 week).
_ROTATION_INTERVAL_MAX_MINUTES = 7 * 24 * 60


def _parse_rotation_intervals_block(
    raw: Any,
    port_base: int,
    num_ports: int,
) -> Dict[int, int]:
    """Per-port rotation interval in minutes; 0 / missing means rotation disabled."""
    out: Dict[int, int] = {}
    if not isinstance(raw, dict) or num_ports <= 0:
        return out
    port_max = port_base + num_ports - 1
    for k, v in raw.items():
        try:
            port = int(str(k))
        except (TypeError, ValueError):
            continue
        if port < port_base or port > port_max:
            continue
        try:
            mins = int(v)
        except (TypeError, ValueError):
            continue
        if mins <= 0:
            continue
        if mins > _ROTATION_INTERVAL_MAX_MINUTES:
            mins = _ROTATION_INTERVAL_MAX_MINUTES
        out[port] = mins
    return out


def _parse_rotation_countries_block(
    raw: Any,
    port_base: int,
    num_ports: int,
) -> Dict[int, str]:
    """Per-port country override (ISO-2 uppercase). Empty / missing means use global randomizeCountry."""
    out: Dict[int, str] = {}
    if not isinstance(raw, dict) or num_ports <= 0:
        return out
    port_max = port_base + num_ports - 1
    for k, v in raw.items():
        try:
            port = int(str(k))
        except (TypeError, ValueError):
            continue
        if port < port_base or port > port_max:
            continue
        s = (str(v) if v is not None else "").strip()
        norm = normalize_randomize_country(s)
        if norm == "random":
            continue
        out[port] = norm
    return out


def _parse_rotation_last_run_block(
    raw: Any,
    port_base: int,
    num_ports: int,
) -> Dict[int, float]:
    """Per-port last rotation unix timestamp (seconds since epoch)."""
    out: Dict[int, float] = {}
    if not isinstance(raw, dict) or num_ports <= 0:
        return out
    port_max = port_base + num_ports - 1
    for k, v in raw.items():
        try:
            port = int(str(k))
        except (TypeError, ValueError):
            continue
        if port < port_base or port > port_max:
            continue
        try:
            ts = float(v)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        out[port] = ts
    return out


def _ingest_assignments_raw(
    raw: Dict[str, Any],
    port_base: int,
    num_ports: int,
    runtime_config: Dict[str, Any],
    cfg_path: Path,
    use_docker: bool,
    source_label: str,
) -> Tuple[
    Dict[int, str],
    List[int],
    Dict[int, str],
    Dict[int, str],
    Dict[int, int],
    Dict[int, str],
    Dict[int, float],
    Dict[int, Dict[str, str]],
    Dict[int, int],
    Dict[int, float],
]:
    """Parse stored JSON blob into compatible OVPN picks plus typed egress state."""
    assignments: Dict[int, str] = {}
    active_listener_ports: List[int] = []
    launcher_ids: Dict[int, str] = {}
    proxy_types: Dict[int, str] = {}
    rotation_intervals: Dict[int, int] = {}
    rotation_countries: Dict[int, str] = {}
    rotation_last_run: Dict[int, float] = {}
    egress_by_port: Dict[int, Dict[str, str]] = {}
    upstream_refresh_intervals: Dict[int, int] = {}
    upstream_refresh_last_run: Dict[int, float] = {}
    if num_ports <= 0:
        return (
            assignments,
            active_listener_ports,
            launcher_ids,
            proxy_types,
            rotation_intervals,
            rotation_countries,
            rotation_last_run,
            egress_by_port,
            upstream_refresh_intervals,
            upstream_refresh_last_run,
        )
    if isinstance(raw, dict) and isinstance(raw.get("assignments"), dict):
        data = raw["assignments"]
    elif isinstance(raw, dict):
        skip = (
            "version",
            "activePorts",
            "launcherIds",
            "proxyTypes",
            "rotationIntervals",
            "rotationCountries",
            "rotationLastRun",
            "egress",
            "upstreamRefreshIntervals",
            "upstreamRefreshLastRun",
        )
        data = {k: v for k, v in raw.items() if str(k) not in skip}
    else:
        return (
            assignments,
            active_listener_ports,
            launcher_ids,
            proxy_types,
            rotation_intervals,
            rotation_countries,
            rotation_last_run,
            egress_by_port,
            upstream_refresh_intervals,
            upstream_refresh_last_run,
        )
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
    egress_by_port = _parse_egress_block(raw.get("egress"), port_base, num_ports, allowed)
    if egress_by_port:
        assignments = _egress_ovpn_assignments(egress_by_port)
    else:
        egress_by_port = _egress_from_assignments(assignments)

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
    launcher_ids = _parse_launcher_ids_block(raw.get("launcherIds"), port_base, num_ports)
    proxy_types = _parse_proxy_types_block(raw.get("proxyTypes"), port_base, num_ports)
    rotation_intervals = _parse_rotation_intervals_block(raw.get("rotationIntervals"), port_base, num_ports)
    rotation_countries = _parse_rotation_countries_block(raw.get("rotationCountries"), port_base, num_ports)
    rotation_last_run = _parse_rotation_last_run_block(raw.get("rotationLastRun"), port_base, num_ports)
    # Last-run timestamps without a matching interval are useless; drop them so persistence stays clean.
    rotation_last_run = {p: ts for p, ts in rotation_last_run.items() if rotation_intervals.get(p, 0) > 0}
    rotation_countries = {p: c for p, c in rotation_countries.items() if rotation_intervals.get(p, 0) > 0}
    upstream_refresh_intervals = _parse_rotation_intervals_block(
        raw.get("upstreamRefreshIntervals"),
        port_base,
        num_ports,
    )
    upstream_refresh_last_run = _parse_rotation_last_run_block(
        raw.get("upstreamRefreshLastRun"),
        port_base,
        num_ports,
    )
    upstream_refresh_last_run = {
        p: ts for p, ts in upstream_refresh_last_run.items() if upstream_refresh_intervals.get(p, 0) > 0
    }
    return (
        assignments,
        sorted(set(active_listener_ports)),
        launcher_ids,
        proxy_types,
        rotation_intervals,
        rotation_countries,
        rotation_last_run,
        egress_by_port,
        upstream_refresh_intervals,
        upstream_refresh_last_run,
    )


def load_gateway_assignments_state(
    path: Path,
    redis_url: str,
    redis_key: str,
    port_base: int,
    num_ports: int,
    runtime_config: Dict[str, Any],
    cfg_path: Path,
    use_docker: bool,
) -> Tuple[
    Dict[int, str],
    List[int],
    Dict[int, str],
    Dict[int, str],
    Dict[int, int],
    Dict[int, str],
    Dict[int, float],
    Dict[int, Dict[str, str]],
    Dict[int, int],
    Dict[int, float],
]:
    """Load OVPN picks + activePorts + launcherIds + proxyTypes + rotation state from Redis or JSON file; migrate file→Redis if needed."""
    if num_ports <= 0:
        return {}, [], {}, {}, {}, {}, {}, {}, {}, {}
    global DB_STORE
    if DB_STORE is not None:
        try:
            raw = DB_STORE.load_assignment_payload(port_base, num_ports)
            return _ingest_assignments_raw(
                raw,
                port_base,
                num_ports,
                runtime_config,
                cfg_path,
                use_docker,
                "postgres port_state",
            )
        except Exception as e:
            _log(f"Could not load assignments from Postgres: {e}")
            return {}, [], {}, {}, {}, {}, {}, {}, {}, {}
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
    launcher_ids: Optional[Dict[int, str]] = None,
    proxy_types: Optional[Dict[int, str]] = None,
    rotation_intervals: Optional[Dict[int, int]] = None,
    rotation_countries: Optional[Dict[int, str]] = None,
    rotation_last_run: Optional[Dict[int, float]] = None,
    egress_by_port: Optional[Dict[int, Dict[str, str]]] = None,
    upstream_refresh_intervals: Optional[Dict[int, int]] = None,
    upstream_refresh_last_run: Optional[Dict[int, float]] = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = assignments_state_payload(
        assignments,
        active_ports,
        launcher_ids,
        proxy_types,
        rotation_intervals,
        rotation_countries,
        rotation_last_run,
        egress_by_port,
        upstream_refresh_intervals,
        upstream_refresh_last_run,
    )
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
    store = state.get("db_store")
    if path is None and store is None:
        return
    p = Path(path) if path is not None else Path("")
    redis_url = (state.get("redis_url") or "").strip()
    redis_key = state.get("redis_state_key") or _redis_state_key()
    mirror_file = os.environ.get("REDIS_ASSIGNMENTS_MIRROR_FILE", "").lower() in ("1", "true", "yes")
    try:
        port_base = int(state["port_base"])
        num_ports = int(state.get("num_ports") or len(state.get("locations") or []))
        with state["lock"]:
            snap_assign = dict(state["port_ovpn_assignment"])
            snap_egress = dict(state.get("port_egress_by_port") or {})
            snap_active = set(state["active_ports"])
            snap_launcher_ids = dict(state.get("launcher_ids_by_port") or {})
            snap_proxy_types = dict(state.get("proxy_types_by_port") or {})
            snap_rot_intervals = dict(state.get("rotation_intervals_by_port") or {})
            snap_rot_countries = dict(state.get("rotation_countries_by_port") or {})
            snap_rot_last_run = dict(state.get("rotation_last_run_by_port") or {})
            snap_refresh_intervals = dict(state.get("upstream_refresh_intervals_by_port") or {})
            snap_refresh_last_run = dict(state.get("upstream_refresh_last_run_by_port") or {})
        if not snap_egress:
            snap_assign = _anti_wipe_merge_assignments(state, snap_assign, port_base, num_ports)
            snap_egress = _egress_from_assignments(snap_assign)
        payload = assignments_state_payload(
            snap_assign,
            snap_active,
            snap_launcher_ids,
            snap_proxy_types,
            snap_rot_intervals,
            snap_rot_countries,
            snap_rot_last_run,
            snap_egress,
            snap_refresh_intervals,
            snap_refresh_last_run,
        )
        if store is not None:
            store.save_assignment_payload(payload, port_base, num_ports)
            return
        if redis_url:
            try:
                _redis_save_json(redis_url, redis_key, payload)
            except Exception as e:
                _log(f"Could not persist assignments to Redis: {e}")
        if not redis_url:
            if p.exists() and not p.is_file():
                _log(f"Cannot persist assignments: path is not a file: {p}")
            else:
                save_port_assignments_file(
                    p,
                    snap_assign,
                    snap_active,
                    snap_launcher_ids,
                    snap_proxy_types,
                    snap_rot_intervals,
                    snap_rot_countries,
                    snap_rot_last_run,
                    snap_egress,
                    snap_refresh_intervals,
                    snap_refresh_last_run,
                )
        elif mirror_file:
            if p.exists() and not p.is_file():
                _log(f"REDIS_ASSIGNMENTS_MIRROR_FILE set but path is not a file: {p}")
            else:
                save_port_assignments_file(
                    p,
                    snap_assign,
                    snap_active,
                    snap_launcher_ids,
                    snap_proxy_types,
                    snap_rot_intervals,
                    snap_rot_countries,
                    snap_rot_last_run,
                    snap_egress,
                    snap_refresh_intervals,
                    snap_refresh_last_run,
                )
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
                "Add ovpn_data:/ovpn to the gateway service in docker-compose.yml and restart."
            )
        ovpn_full = (mount / ovpn_name).resolve()
        if not _is_safe_under_root(ovpn_full, mount):
            return f"OVPN path escapes VPN volume: {ovpn_name}"
        if not ovpn_full.exists() or not ovpn_full.is_file():
            return (
                f"OVPN file not found in VPN folder: {ovpn_name}. "
                "Upload it from the dashboard into the OVPN volume and try again."
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
            load_provider_auth(
                ovpn_name,
                mount,
                config.get("_providerCredentials") if isinstance(config.get("_providerCredentials"), dict) else None,
            )
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


def validate_port_egress(
    config: Dict[str, Any],
    config_path: Path,
    location_index: int,
    use_docker: bool,
    egress: Dict[str, str],
    upstream_profiles_by_id: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    egress_type = (egress.get("type") or "").strip().lower()
    if egress_type == "ovpn":
        return validate_location_assets(
            config,
            config_path,
            location_index,
            use_docker,
            (egress.get("ovpn") or "").strip(),
        )
    if egress_type == "upstream":
        profile_id = (egress.get("upstreamProxyId") or "").strip()
        if not profile_id:
            return "Select an upstream proxy for this port before activation"
        if profile_id not in upstream_profiles_by_id:
            return f"Upstream proxy profile not found: {profile_id}"
        return None
    return "Select an OVPN profile or upstream proxy for this port before activation"


def _public_egress(
    egress: Optional[Dict[str, str]],
    upstream_profiles_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    if not egress:
        return {"type": "none"}
    egress_type = (egress.get("type") or "").strip().lower()
    if egress_type == "ovpn":
        return {"type": "ovpn", "ovpn": (egress.get("ovpn") or "").strip()}
    if egress_type == "upstream":
        profile_id = (egress.get("upstreamProxyId") or "").strip()
        public = {"type": "upstream", "upstreamProxyId": profile_id}
        profile = upstream_profiles_by_id.get(profile_id)
        if profile:
            public["upstreamProxy"] = public_profile(profile)
        return public
    return {"type": "none"}


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
        proxy_running = pp is not None and pp.poll() is None
        if slot.get("egress_type") == "upstream":
            return proxy_running
        return op is not None and op.poll() is None and proxy_running


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


def _deactivate_listener_port_unlocked(state: Dict[str, Any], port: int) -> None:
    """Caller must hold state['lock']. Tear down worker for this listener port and mark inactive."""
    port_to_slot = state["port_to_slot"]
    port_base = state["port_base"]
    use_docker = state["use_docker"]
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


def deactivate_listener_port(state: Dict[str, Any], port: int) -> None:
    with state["lock"]:
        _deactivate_listener_port_unlocked(state, port)


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


def _relay_existing_sockets(
    client_sock: socket.socket,
    backend_sock: socket.socket,
    slot: Optional[Dict[str, Any]],
    lock: threading.Lock,
) -> None:
    try:
        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        backend_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    client_sock.settimeout(300)
    backend_sock.settimeout(300)

    def update_activity() -> None:
        if slot and lock:
            with lock:
                slot["last_activity"] = time.monotonic()

    def pump(a: socket.socket, b: socket.socket) -> bool:
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
                elif not pump(backend_sock, client_sock):
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


def _auth_route_launch_config(state: Dict[str, Any]) -> Dict[str, Any]:
    config = dict(state.get("auth_runtime_config") or {})
    routes = list(state.get("auth_routes") or [])
    config["locations"] = _auth_route_location_config(routes)
    config["internalProxyAuthEnabled"] = False
    config["proxyUsername"] = ""
    config["proxyPassword"] = ""
    return config


def _refresh_upstream_session_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Return a runtime-only profile copy with a fresh session token when present."""
    refreshed = dict(profile)
    username = str(refreshed.get("username") or "")

    def replace_session(match: re.Match[str]) -> str:
        token = str(secrets.randbelow(9000000000) + 1000000000)
        return f"{match.group(1)}session{match.group(2)}{token}"

    refreshed["username"] = re.sub(
        r"(?i)(^|[-_])session([-_])([A-Za-z0-9]+)",
        replace_session,
        username,
        count=1,
    )
    return refreshed


def _start_auth_route_backend(
    state: Dict[str, Any],
    route_index: int,
    scheme: str,
    refresh_upstream_session: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    routes = list(state.get("auth_routes") or [])
    if route_index < 0 or route_index >= len(routes):
        return None, "Route not found"
    route = routes[route_index]
    if not bool(route.get("enabled", True)):
        return None, "Route is disabled"
    scheme = "socks5" if scheme == "socks5" else "http"
    route_proxy_type = "socks5" if route.get("proxyType") == "socks5" else "http"
    if scheme != route_proxy_type:
        return None, f"Route is configured for {route_proxy_type.upper()} only"
    backend_key = _auth_route_backend_key(route_index, scheme)
    use_docker = bool(state.get("use_docker"))
    with state["lock"]:
        existing = state["port_to_slot"].get(backend_key)
        if existing and is_backend_running(existing, use_docker):
            existing["last_activity"] = time.monotonic()
            return existing, None
        if existing and state.setdefault("auth_route_state", {}).get(f"{route.get('username')}:{scheme}") == "starting":
            return existing, None
        if existing and not is_backend_running(existing, use_docker):
            state["port_to_slot"].pop(backend_key, None)
            teardown_slot(existing, use_docker)
            existing["external_port"] = None
            existing["location_index"] = None

    if use_docker:
        container_state = _auth_route_docker_container_state(backend_key)
        if container_state.get("running"):
            with state["lock"]:
                slot = state["port_to_slot"].get(backend_key)
                if slot is None:
                    slot = _make_auth_route_slot(state)
                    if slot is None:
                        return None, "No available slot capacity"
                slot["location_index"] = route_index
                slot["external_port"] = backend_key
                slot["proxy_type"] = scheme
                slot["route_username"] = route.get("username") or ""
                slot["backend_host"] = container_state.get("name") or _auth_route_container_name(backend_key)
                slot["backend_port"] = BACKEND_HTTP_PORT
                slot["container_name"] = container_state.get("name") or _auth_route_container_name(backend_key)
                slot["last_activity"] = time.monotonic()
                slot["egress_type"] = (route.get("egress") or {}).get("type")
                state["port_to_slot"][backend_key] = slot
                state.setdefault("auth_route_state", {})[f"{route.get('username')}:{scheme}"] = "active"
                state.setdefault("auth_route_error", {}).pop(f"{route.get('username')}:{scheme}", None)
                state.setdefault("auth_route_egress_ip", {}).pop(f"{route.get('username')}:{scheme}", None)
            _maybe_start_auth_route_egress_ip_refresh(state, route.get("username") or "", scheme, slot, force=True)
            return slot, None
        if container_state.get("exists"):
            _remove_auth_route_docker_container(backend_key)

    launch_config = _auth_route_launch_config(state)
    egress = dict(route.get("egress") or {})
    upstream_profiles = state.get("upstream_profiles_by_id") or {}
    upstream_profile = upstream_profiles.get((egress.get("upstreamProxyId") or "").strip())
    if refresh_upstream_session and egress.get("type") == "upstream" and upstream_profile:
        upstream_profile = _refresh_upstream_session_profile(upstream_profile)
    validation_err = validate_port_egress(
        launch_config,
        state["config_path"],
        route_index,
        use_docker,
        egress,
        upstream_profiles,
    )
    if validation_err:
        return None, validation_err

    with state["lock"]:
        slot = state["port_to_slot"].get(backend_key)
        if slot is not None and state.setdefault("auth_route_state", {}).get(f"{route.get('username')}:{scheme}") == "starting":
            return slot, None
        if slot is None:
            slot = _make_auth_route_slot(state)
        if slot is None:
            return None, "No available slot capacity"
        slot["location_index"] = route_index
        slot["external_port"] = backend_key
        slot["proxy_type"] = scheme
        slot["route_username"] = route.get("username") or ""
        state["port_to_slot"][backend_key] = slot
        state.setdefault("auth_route_state", {})[f"{route.get('username')}:{scheme}"] = "starting"

    try:
        if use_docker:
            from backend_docker import start_docker_backend

            backend_host, _ = start_docker_backend(
                route_index,
                backend_key,
                launch_config,
                state.get("docker_image") or os.environ.get("DOCKER_IMAGE", "portico-worker"),
                state.get("docker_network") or os.environ.get("DOCKER_NETWORK", "proxynet"),
                state.get("ovpn_volume_name") or os.environ.get("DOCKER_OVPN_VOLUME", "ovpn_data"),
                proxy_listen_scheme=scheme,
                upstream_profile=upstream_profile if egress.get("type") == "upstream" else None,
            )
            with state["lock"]:
                slot["backend_host"] = backend_host
                slot["backend_port"] = BACKEND_HTTP_PORT
                slot["container_name"] = backend_host
                slot["last_activity"] = time.monotonic()
                slot["egress_type"] = egress.get("type")
            if not wait_for_backend(backend_host, BACKEND_HTTP_PORT):
                teardown_slot(slot, use_docker)
                with state["lock"]:
                    state["port_to_slot"].pop(backend_key, None)
                    state.setdefault("auth_route_state", {})[f"{route.get('username')}:{scheme}"] = "failed"
                    state.setdefault("auth_route_error", {})[f"{route.get('username')}:{scheme}"] = "Backend did not become ready in time"
                return None, "Backend did not become ready in time"
        else:
            if egress.get("type") == "upstream" and upstream_profile:
                openvpn_process = None
                proxy_process = start_one_upstream_proxy(
                    launch_config,
                    slot["internal_port"],
                    upstream_profile,
                    listen_scheme=scheme,
                )
                log_path = auth_path = ""
            else:
                openvpn_process, proxy_process, log_path, auth_path = start_one_location(
                    launch_config,
                    route_index,
                    slot["internal_port"],
                    state["config_path"],
                    listen_scheme=scheme,
                )
            with state["lock"]:
                slot["openvpn_process"] = openvpn_process
                slot["proxy_process"] = proxy_process
                slot["log_path"] = log_path
                slot["auth_path"] = auth_path
                slot["backend_host"] = "127.0.0.1"
                slot["backend_port"] = slot["internal_port"]
                slot["last_activity"] = time.monotonic()
                slot["egress_type"] = egress.get("type")
            if not wait_for_backend("127.0.0.1", slot["internal_port"]):
                teardown_slot(slot, use_docker)
                with state["lock"]:
                    state["port_to_slot"].pop(backend_key, None)
                    state.setdefault("auth_route_state", {})[f"{route.get('username')}:{scheme}"] = "failed"
                    state.setdefault("auth_route_error", {})[f"{route.get('username')}:{scheme}"] = "Backend did not become ready in time"
                return None, "Backend did not become ready in time"
    except Exception as e:
        with state["lock"]:
            teardown_slot(slot, use_docker)
            state["port_to_slot"].pop(backend_key, None)
            state.setdefault("auth_route_state", {})[f"{route.get('username')}:{scheme}"] = "failed"
            state.setdefault("auth_route_error", {})[f"{route.get('username')}:{scheme}"] = str(e)
        return None, str(e)

    with state["lock"]:
        state.setdefault("auth_route_state", {})[f"{route.get('username')}:{scheme}"] = "active"
        state.setdefault("auth_route_error", {}).pop(f"{route.get('username')}:{scheme}", None)
        state.setdefault("auth_route_egress_ip", {}).pop(f"{route.get('username')}:{scheme}", None)
    _maybe_start_auth_route_egress_ip_refresh(state, route.get("username") or "", scheme, slot, force=True)
    return slot, None


def _stop_auth_route_backends(
    state: Dict[str, Any],
    username: str,
    scheme: str = "both",
) -> bool:
    routes = list(state.get("auth_routes") or [])
    found = _auth_route_by_username(routes, username)
    if not found:
        return False
    route_index, route = found
    schemes = ["http", "socks5"] if scheme == "both" else ["socks5" if scheme == "socks5" else "http"]
    use_docker = bool(state.get("use_docker"))
    with state["lock"]:
        for item in schemes:
            key = _auth_route_backend_key(route_index, item)
            slot = state["port_to_slot"].pop(key, None)
            if slot is not None:
                teardown_slot(slot, use_docker)
            if use_docker:
                _remove_auth_route_docker_container(key)
            state.setdefault("auth_route_state", {})[f"{route.get('username')}:{item}"] = "inactive"
            state.setdefault("auth_route_error", {}).pop(f"{route.get('username')}:{item}", None)
            state.setdefault("auth_route_egress_ip", {}).pop(f"{route.get('username')}:{item}", None)
            state.setdefault("auth_route_egress_ip_refreshing", set()).discard(f"{route.get('username')}:{item}")
    return True


def _read_http_proxy_initial(client_sock: socket.socket) -> bytes:
    data = b""
    client_sock.settimeout(5)
    while b"\r\n\r\n" not in data and len(data) < BUFFER_SIZE:
        chunk = client_sock.recv(4096)
        if not chunk:
            break
        data += chunk
    client_sock.settimeout(300)
    return data


def parse_http_proxy_basic_auth(initial_data: bytes) -> Tuple[str, str, Optional[str]]:
    header_block = initial_data.split(b"\r\n\r\n", 1)[0]
    auth_value = ""
    for raw_line in header_block.split(b"\r\n")[1:]:
        if raw_line.lower().startswith(b"proxy-authorization:"):
            auth_value = raw_line.split(b":", 1)[1].decode("latin-1", errors="replace").strip()
            break
    if not auth_value:
        return "", "", "Missing Proxy-Authorization header"
    scheme, _, token = auth_value.partition(" ")
    if scheme.lower() != "basic" or not token.strip():
        return "", "", "Proxy-Authorization must use Basic auth"
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except Exception:
        return "", "", "Invalid Basic auth token"
    username, sep, password = decoded.partition(":")
    if not sep or not username:
        return "", "", "Basic auth must contain username and password"
    return username, password, None


def strip_proxy_authorization_header(initial_data: bytes) -> bytes:
    head, sep, tail = initial_data.partition(b"\r\n\r\n")
    if not sep:
        return initial_data
    lines = head.split(b"\r\n")
    filtered = [lines[0]]
    filtered.extend(
        line for line in lines[1:] if not line.lower().startswith(b"proxy-authorization:")
    )
    return b"\r\n".join(filtered) + sep + tail


def _send_http_proxy_auth_required(client_sock: socket.socket, message: str = "Proxy authentication required") -> None:
    body = (message + "\n").encode("utf-8")
    resp = (
        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
        b'Proxy-Authenticate: Basic realm="Portico"\r\n'
        b"Content-Type: text/plain; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
        + body
    )
    try:
        client_sock.sendall(resp)
    except Exception:
        pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("connection closed")
        data += chunk
    return data


def _read_socks5_request(sock: socket.socket) -> bytes:
    head = _recv_exact(sock, 4)
    if len(head) != 4 or head[0] != 5:
        raise ValueError("Invalid SOCKS5 request")
    atyp = head[3]
    if atyp == 1:
        addr = _recv_exact(sock, 4)
    elif atyp == 3:
        ln = _recv_exact(sock, 1)
        addr = ln + _recv_exact(sock, ln[0])
    elif atyp == 4:
        addr = _recv_exact(sock, 16)
    else:
        raise ValueError("Unsupported SOCKS5 address type")
    port = _recv_exact(sock, 2)
    return head + addr + port


def _read_socks5_response(sock: socket.socket) -> bytes:
    return _read_socks5_request(sock)


def _send_socks5_reply(sock: socket.socket, code: int) -> None:
    try:
        sock.sendall(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
    except Exception:
        pass


def _auth_route_for_credentials(
    state: Dict[str, Any],
    username: str,
    password: str,
    scheme: str,
) -> Tuple[Optional[int], Optional[Dict[str, Any]], Optional[str]]:
    if not _route_password_matches(state.get("auth_global_password") or "", password):
        return None, None, "Invalid username or password"
    found = _auth_route_by_username(state.get("auth_routes") or [], username)
    if not found:
        return None, None, "Unknown route username"
    route_index, route = found
    if not bool(route.get("enabled", True)):
        return None, None, "Route is disabled"
    route_proxy_type = "socks5" if route.get("proxyType") == "socks5" else "http"
    requested_scheme = "socks5" if scheme == "socks5" else "http"
    if route_proxy_type != requested_scheme:
        return None, None, f"Route is configured for {route_proxy_type.upper()} only"
    return route_index, route, None


def handle_auth_http_connection(client_sock: socket.socket, state: Dict[str, Any]) -> None:
    try:
        initial_data = _read_http_proxy_initial(client_sock)
        username, password, auth_err = parse_http_proxy_basic_auth(initial_data)
        if auth_err:
            _send_http_proxy_auth_required(client_sock, auth_err)
            return
        route_index, route, route_err = _auth_route_for_credentials(state, username, password, "http")
        if route_err or route is None or route_index is None:
            _send_http_proxy_auth_required(client_sock, route_err or "Proxy authentication failed")
            return
        slot, start_err = _start_auth_route_backend(state, route_index, "http")
        if start_err or slot is None:
            _send_http_proxy_auth_required(client_sock, start_err or "Route backend unavailable")
            return
        backend_host = slot.get("backend_host")
        backend_port = slot.get("backend_port")
        if not backend_host or not backend_port:
            _send_http_proxy_auth_required(client_sock, "Route backend unavailable")
            return
        forward(
            client_sock,
            str(backend_host),
            int(backend_port),
            strip_proxy_authorization_header(initial_data),
            slot,
            state["lock"],
        )
    finally:
        try:
            client_sock.close()
        except Exception:
            pass


def handle_auth_socks_connection(client_sock: socket.socket, state: Dict[str, Any]) -> None:
    backend_sock: Optional[socket.socket] = None
    try:
        client_sock.settimeout(30)
        greeting = _recv_exact(client_sock, 2)
        if len(greeting) != 2 or greeting[0] != 5:
            return
        methods = _recv_exact(client_sock, greeting[1])
        if 2 not in methods:
            client_sock.sendall(b"\x05\xff")
            return
        client_sock.sendall(b"\x05\x02")
        auth_head = _recv_exact(client_sock, 2)
        if len(auth_head) != 2 or auth_head[0] != 1:
            client_sock.sendall(b"\x01\x01")
            return
        username = _recv_exact(client_sock, auth_head[1]).decode("utf-8", errors="replace")
        pass_len = _recv_exact(client_sock, 1)[0]
        password = _recv_exact(client_sock, pass_len).decode("utf-8", errors="replace")
        route_index, route, route_err = _auth_route_for_credentials(state, username, password, "socks5")
        if route_err or route is None or route_index is None:
            client_sock.sendall(b"\x01\x01")
            return
        client_sock.sendall(b"\x01\x00")
        request = _read_socks5_request(client_sock)
        if request[1] != 1:
            _send_socks5_reply(client_sock, 7)
            return
        slot, start_err = _start_auth_route_backend(state, route_index, "socks5")
        if start_err or slot is None:
            _send_socks5_reply(client_sock, 1)
            return
        backend_host = slot.get("backend_host")
        backend_port = slot.get("backend_port")
        if not backend_host or not backend_port:
            _send_socks5_reply(client_sock, 1)
            return

        backend_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        backend_sock.settimeout(30)
        backend_sock.connect((str(backend_host), int(backend_port)))
        backend_sock.sendall(b"\x05\x01\x00")
        backend_greeting = _recv_exact(backend_sock, 2)
        if backend_greeting != b"\x05\x00":
            _send_socks5_reply(client_sock, 1)
            return
        backend_sock.sendall(request)
        response = _read_socks5_response(backend_sock)
        client_sock.sendall(response)
        if len(response) >= 2 and response[1] != 0:
            return
        _relay_existing_sockets(client_sock, backend_sock, slot, state["lock"])
        backend_sock = None
    except Exception:
        pass
    finally:
        try:
            client_sock.close()
        except Exception:
            pass
        if backend_sock is not None:
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
    port_egress_by_port: Dict[int, Dict[str, str]],
    upstream_profiles_by_id: Dict[str, Dict[str, Any]],
    activation_state_by_port: Dict[int, str],
    lock: threading.Lock,
    use_docker: bool = False,
    docker_image: str = "",
    docker_network: str = "proxynet",
    ovpn_volume_name: str = "ovpn_data",
    proxy_types_by_port: Optional[Dict[int, str]] = None,
) -> None:
    proxy_types_by_port = proxy_types_by_port or {}
    listen_scheme = (proxy_types_by_port.get(external_port) or "http").strip().lower()
    if listen_scheme not in ("http", "socks5"):
        listen_scheme = "http"
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
        egress = dict(port_egress_by_port.get(external_port) or {})
        assigned_ovpn = (port_ovpn_assignment.get(external_port) or "").strip()
        upstream_profile = upstream_profiles_by_id.get((egress.get("upstreamProxyId") or "").strip())
        if not egress or (egress.get("type") == "ovpn" and not assigned_ovpn) or (
            egress.get("type") == "upstream" and not upstream_profile
        ):
            _log(f"Rejecting connection on port {external_port}: no usable egress assignment")
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
                "proxy_type": None,
                "egress_type": None,
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
    if egress.get("type") == "ovpn" and 0 <= location_index < len(launch_locations):
        launch_locations[location_index]["ovpn"] = assigned_ovpn
    launch_config["locations"] = launch_locations
    if use_docker:
        _log(f"Starting Docker worker for location {location_index} port {first_port} scheme={listen_scheme}")
        try:
            from backend_docker import start_docker_backend
            backend_host, _ = start_docker_backend(
                location_index, first_port, launch_config,
                docker_image, docker_network, ovpn_volume_name,
                proxy_listen_scheme=listen_scheme,
                upstream_profile=upstream_profile if egress.get("type") == "upstream" else None,
            )
            _log(f"Docker worker started: {backend_host} ({listen_scheme.upper()}:{BACKEND_HTTP_PORT})")
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
            slot["proxy_type"] = listen_scheme
            slot["egress_type"] = egress.get("type")
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
                            _log("Hint: Upload OVPN files from the dashboard into the ovpn_data volume.")
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
        _log(
            f"Starting local backend for location {location_index} port {external_port} "
            f"internal_port={slot['internal_port']} scheme={listen_scheme}"
        )
        try:
            if egress.get("type") == "upstream" and upstream_profile:
                openvpn_process = None
                proxy_process = start_one_upstream_proxy(
                    launch_config,
                    slot["internal_port"],
                    upstream_profile,
                    listen_scheme=listen_scheme,
                )
                log_path = auth_path = ""
            else:
                openvpn_process, proxy_process, log_path, auth_path = start_one_location(
                    launch_config, location_index, slot["internal_port"], config_path,
                    listen_scheme=listen_scheme,
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
            slot["proxy_type"] = listen_scheme
            slot["egress_type"] = egress.get("type")

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
            evicted_ports = [
                s.get("external_port")
                if s.get("external_port") is not None
                else port_base + s["location_index"]
                for s in to_evict
            ]
            for slot in to_evict:
                ep = slot.get("external_port")
                loc = slot.get("location_index")
                if ep is not None:
                    port_to_slot.pop(ep, None)
                elif loc is not None:
                    port_to_slot.pop(port_base + loc, None)
                teardown_slot(slot, use_docker)
                slot["external_port"] = None
                slot["location_index"] = None
            for ep in evicted_ports:
                if state.get("auth_routing"):
                    for route in state.get("auth_routes") or []:
                        for scheme in ("http", "socks5"):
                            if ep == _auth_route_backend_key(int(route.get("index") or 0), scheme):
                                state.setdefault("auth_route_state", {})[f"{route.get('username')}:{scheme}"] = "inactive"
                                state.setdefault("auth_route_error", {}).pop(f"{route.get('username')}:{scheme}", None)
                else:
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
    port_egress_by_port: Dict[int, Dict[str, str]],
    upstream_profiles_by_id: Dict[str, Dict[str, Any]],
    lock: threading.Lock,
    use_docker: bool = False,
    docker_image: str = "",
    docker_network: str = "proxynet",
    ovpn_volume_name: str = "ovpn_data",
    listen_scheme: str = "http",
) -> Optional[str]:
    location_index = port - port_base
    locations = config.get("locations") or []
    if location_index < 0 or location_index >= len(locations):
        return "Port out of location range"

    assigned_ovpn = (port_ovpn_assignment.get(port) or "").strip()
    egress = dict(port_egress_by_port.get(port) or {})
    upstream_profile = upstream_profiles_by_id.get((egress.get("upstreamProxyId") or "").strip())
    if not egress:
        return "Select an OVPN profile or upstream proxy for this port before activation"
    if egress.get("type") == "ovpn" and not assigned_ovpn:
        return "Select an OVPN file for this port before activation"
    if egress.get("type") == "upstream" and not upstream_profile:
        return "Select a valid upstream proxy for this port before activation"

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
                "proxy_type": None,
                "egress_type": None,
            }
            slots.append(slot)
        slot["location_index"] = location_index
        slot["external_port"] = port
        port_to_slot[port] = slot

    ls = (listen_scheme or "http").strip().lower()
    if ls not in ("http", "socks5"):
        ls = "http"

    launch_config = dict(config)
    launch_locations = [dict(loc) for loc in (config.get("locations") or [])]
    if egress.get("type") == "ovpn":
        launch_locations[location_index]["ovpn"] = assigned_ovpn
    launch_config["locations"] = launch_locations

    if use_docker:
        try:
            from backend_docker import start_docker_backend
            backend_host, _ = start_docker_backend(
                location_index, port, launch_config, docker_image, docker_network, ovpn_volume_name,
                proxy_listen_scheme=ls,
                upstream_profile=upstream_profile if egress.get("type") == "upstream" else None,
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
            slot["proxy_type"] = ls
            slot["egress_type"] = egress.get("type")
        if not wait_for_backend(backend_host, BACKEND_HTTP_PORT):
            teardown_slot(slot, use_docker)
            with lock:
                slot["external_port"] = None
                slot["location_index"] = None
                port_to_slot.pop(port, None)
            return "Docker worker did not become ready in time"
        return None

    try:
        if egress.get("type") == "upstream" and upstream_profile:
            openvpn_process = None
            proxy_process = start_one_upstream_proxy(
                launch_config,
                slot["internal_port"],
                upstream_profile,
                listen_scheme=ls,
            )
            log_path = auth_path = ""
        else:
            openvpn_process, proxy_process, log_path, auth_path = start_one_location(
                launch_config, location_index, slot["internal_port"], config_path,
                listen_scheme=ls,
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
        slot["proxy_type"] = ls
        slot["egress_type"] = egress.get("type")

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
    with state["lock"]:
        listen_scheme = (state.get("proxy_types_by_port") or {}).get(port) or "http"
    if listen_scheme not in ("http", "socks5"):
        listen_scheme = "http"
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
        port_egress_by_port=state["port_egress_by_port"],
        upstream_profiles_by_id=state["upstream_profiles_by_id"],
        lock=state["lock"],
        use_docker=bool(state.get("use_docker")),
        docker_image=runtime_config.get("dockerImage") or os.environ.get("DOCKER_IMAGE", "portico-worker"),
        docker_network=runtime_config.get("dockerNetwork") or os.environ.get("DOCKER_NETWORK", "proxynet"),
        ovpn_volume_name=runtime_config.get("dockerOvpnVolume") or os.environ.get("DOCKER_OVPN_VOLUME", "ovpn_data"),
        listen_scheme=listen_scheme,
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


def _pick_rotation_ovpn(
    runtime_config: Dict[str, Any],
    config_path: Path,
    use_docker: bool,
    country_override: str,
    current_ovpn: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Return (chosen_ovpn, error). Filter allowed list by country (override or global), pick random different from current."""
    allowed = list_allowed_ovpn_files(runtime_config, config_path, use_docker)
    if not allowed:
        return None, "No .ovpn files available to rotate"
    rc = (country_override or "").strip()
    if not rc:
        rc = normalize_randomize_country(runtime_config.get("randomizeCountry"))
    if rc != "random":
        filtered = filter_ovpn_files_by_country(allowed, rc)
        if not filtered:
            return None, f"No .ovpn files for country {rc} (rotation pool is empty)"
        allowed = filtered
    pool = list(allowed)
    if len(pool) > 1 and current_ovpn:
        others = [f for f in pool if f != current_ovpn]
        if others:
            pool = others
    return secrets.choice(pool), None


def _perform_port_rotation_to(
    state: Dict[str, Any],
    port: int,
    new_ovpn: str,
    runtime_config: Dict[str, Any],
    config_path: Path,
) -> Optional[str]:
    """Tear down active slot (if any), reassign ovpn, validate, restart asynchronously.

    Returns None on success or error string on failure. Caller must not hold state['lock'].
    """
    port_base = state["port_base"]
    locations = state["locations"]
    loc_idx = port - port_base
    if loc_idx < 0 or loc_idx >= len(locations):
        return "Port out of location range"
    use_docker = bool(state.get("use_docker"))
    lock = state["lock"]
    port_to_slot = state["port_to_slot"]

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
        state["port_ovpn_assignment"][port] = new_ovpn
        state.setdefault("port_egress_by_port", {})[port] = {"type": "ovpn", "ovpn": new_ovpn}
    persist_assignments_snapshot(state)

    err = validate_location_assets(
        runtime_config, config_path, loc_idx, use_docker, new_ovpn,
    )
    if err:
        with lock:
            state["port_ovpn_assignment"].pop(port, None)
            state["activation_cancelled_ports"].discard(port)
        persist_assignments_snapshot(state)
        return err

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
    return None


def _perform_auth_route_rotation_to(
    state: Dict[str, Any],
    username: str,
    new_ovpn: str,
    runtime_config: Dict[str, Any],
    config_path: Path,
) -> Optional[str]:
    """Update an auth route OVPN and restart that username's selected active backend."""
    use_docker = bool(state.get("use_docker"))
    with state["lock"]:
        routes = [dict(r) for r in (state.get("auth_routes") or [])]
        found = _auth_route_by_username(routes, username)
        if not found:
            return "Route not found"
        route_index, route = found
        if not bool(route.get("enabled", True)):
            return "Route is disabled"
        if (route.get("egress") or {}).get("type") != "ovpn":
            return "Route is not an OpenVPN route"
        scheme = "socks5" if route.get("proxyType") == "socks5" else "http"
        backend_key = _auth_route_backend_key(route_index, scheme)
        slot = state.get("port_to_slot", {}).get(backend_key)
        was_active = bool(slot and is_backend_running(slot, use_docker)) or (
            state.get("auth_route_state", {}).get(f"{username}:{scheme}") == "active"
        )

    updated_routes = [dict(r) for r in routes]
    updated_route = dict(updated_routes[route_index])
    updated_route["egress"] = {"type": "ovpn", "ovpn": new_ovpn}
    updated_route["rotationLastRun"] = time.time()
    updated_routes[route_index] = updated_route

    launch_config = dict(runtime_config)
    launch_config["locations"] = _auth_route_location_config(updated_routes)
    validation_err = validate_port_egress(
        launch_config,
        config_path,
        route_index,
        use_docker,
        {"type": "ovpn", "ovpn": new_ovpn},
        state.get("upstream_profiles_by_id") or {},
    )
    if validation_err:
        return validation_err

    save_err = _persist_auth_routes_config(config_path, state, updated_routes)
    if save_err:
        return save_err

    if was_active:
        _stop_auth_route_backends(state, username, scheme)

    with state["lock"]:
        for i, row in enumerate(updated_routes):
            row["index"] = i
        state["auth_routes"] = updated_routes
        runtime = dict(state.get("auth_runtime_config") or {})
        auth_cfg = dict(_auth_routing_dict(runtime))
        auth_cfg.update(
            {
                "enabled": True,
                "httpPort": state.get("auth_http_port"),
                "socksPort": state.get("auth_socks_port"),
                "routes": updated_routes,
            }
        )
        runtime["authRouting"] = auth_cfg
        state["auth_runtime_config"] = runtime

    if was_active:
        _slot, start_err = _start_auth_route_backend(state, route_index, scheme)
        if start_err:
            return start_err
    return None


def _perform_port_egress_change_to(
    state: Dict[str, Any],
    port: int,
    new_egress: Dict[str, str],
    runtime_config: Dict[str, Any],
    config_path: Path,
) -> Optional[str]:
    """Set typed egress and restart an active port so the change applies immediately."""
    port_base = state["port_base"]
    loc_idx = port - port_base
    locations = state["locations"]
    if loc_idx < 0 or loc_idx >= len(locations):
        return "Port out of location range"

    egress_type = (new_egress.get("type") or "none").strip().lower()
    normalized: Dict[str, str]
    if egress_type == "ovpn":
        normalized = {"type": "ovpn", "ovpn": (new_egress.get("ovpn") or "").strip()}
    elif egress_type == "upstream":
        normalized = {
            "type": "upstream",
            "upstreamProxyId": (new_egress.get("upstreamProxyId") or "").strip(),
        }
    elif egress_type == "none":
        normalized = {"type": "none"}
    else:
        return 'type must be "ovpn", "upstream", or "none"'

    use_docker = bool(state.get("use_docker"))
    if normalized["type"] != "none":
        err = validate_port_egress(
            runtime_config,
            config_path,
            loc_idx,
            use_docker,
            normalized,
            state.get("upstream_profiles_by_id") or {},
        )
        if err:
            return err

    lock = state["lock"]
    with lock:
        was_active = (
            port in state["active_ports"]
            or state["activation_state_by_port"].get(port) in ("starting", "active")
        )
        _deactivate_listener_port_unlocked(state, port)

        egress_by_port = state.setdefault("port_egress_by_port", {})
        if normalized["type"] == "none":
            egress_by_port.pop(port, None)
            state["port_ovpn_assignment"].pop(port, None)
            state.setdefault("rotation_intervals_by_port", {}).pop(port, None)
            state.setdefault("rotation_countries_by_port", {}).pop(port, None)
            state.setdefault("rotation_last_run_by_port", {}).pop(port, None)
            state.setdefault("upstream_refresh_intervals_by_port", {}).pop(port, None)
            state.setdefault("upstream_refresh_last_run_by_port", {}).pop(port, None)
        else:
            egress_by_port[port] = normalized
            if normalized["type"] == "ovpn":
                state["port_ovpn_assignment"][port] = normalized["ovpn"]
                state.setdefault("upstream_refresh_intervals_by_port", {}).pop(port, None)
                state.setdefault("upstream_refresh_last_run_by_port", {}).pop(port, None)
            else:
                state["port_ovpn_assignment"].pop(port, None)
                state.setdefault("rotation_intervals_by_port", {}).pop(port, None)
                state.setdefault("rotation_countries_by_port", {}).pop(port, None)
                state.setdefault("rotation_last_run_by_port", {}).pop(port, None)

        if was_active and normalized["type"] != "none":
            state["activation_cancelled_ports"].discard(port)
            state["active_ports"].add(port)
            state["activation_state_by_port"][port] = "starting"
            state["activation_error_by_port"].pop(port, None)

    persist_assignments_snapshot(state)
    if was_active and normalized["type"] != "none":
        threading.Thread(
            target=_activate_port_async,
            args=(port, runtime_config, state),
            daemon=True,
        ).start()
    return None


def _perform_port_location_change(
    state: Dict[str, Any],
    port: int,
    runtime_config: Dict[str, Any],
    config_path: Path,
    requested_ovpn: str = "",
    requested_country: str = "",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Change a listener to an exact OVPN or random country pick; active ports restart."""
    port_base = state["port_base"]
    locations = state["locations"]
    loc_idx = port - port_base
    if loc_idx < 0 or loc_idx >= len(locations):
        return None, "Port out of location range"

    selected = (requested_ovpn or "").strip()
    country_raw = (requested_country or "").strip()
    if selected and country_raw:
        return None, "Provide either ovpn or country, not both"
    if not selected and not country_raw:
        return None, "Provide ovpn or country"

    use_docker = bool(state.get("use_docker"))
    current = ""
    with state["lock"]:
        current = (state.get("port_ovpn_assignment") or {}).get(port, "") or ""

    if selected:
        if Path(selected).suffix.lower() != ".ovpn":
            return None, "Only .ovpn files are allowed"
        if not _is_safe_relative_ovpn_name(selected):
            return None, "ovpn must be a safe relative path"
    else:
        country_norm = normalize_randomize_country(country_raw)
        if country_norm == "random" and country_raw.lower() not in ("random", ""):
            return None, 'country must be a 2-letter ISO code or "random"'
        allowed = list_allowed_ovpn_files(runtime_config, config_path, use_docker)
        if not allowed:
            return None, "No .ovpn files available"
        pool = allowed
        if country_norm != "random":
            pool = filter_ovpn_files_by_country(allowed, country_norm)
            if not pool:
                return None, f"No .ovpn files for country {country_norm}"
        if len(pool) > 1 and current:
            others = [name for name in pool if name != current]
            if others:
                pool = others
        selected = secrets.choice(list(pool))

    err = _perform_port_egress_change_to(
        state,
        port,
        {"type": "ovpn", "ovpn": selected},
        runtime_config,
        config_path,
    )
    if err:
        return None, err
    with state["lock"]:
        activation_state = state["activation_state_by_port"].get(port, "inactive")
    return (
        {
            "ok": True,
            "port": port,
            "ovpn": selected,
            "locationIndex": loc_idx,
            "activationState": activation_state,
        },
        None,
    )


# Tick interval for the rotation worker thread. Smaller than the smallest sane rotation interval.
ROTATION_TICK_SECONDS = 5.0


def rotation_loop(state: Dict[str, Any]) -> None:
    """Background worker: rotate active ports whose interval has elapsed.

    Only ports with rotation_intervals_by_port[port] > 0 AND activation_state == 'active'
    are rotated. Uses wall-clock timestamps so the schedule survives gateway restarts.
    """
    global shutdown_flag
    config_path = state["config_path"]
    while not shutdown_flag:
        time.sleep(ROTATION_TICK_SECONDS)
        if shutdown_flag:
            break
        try:
            now_wall = time.time()
            with state["lock"]:
                intervals = dict(state.get("rotation_intervals_by_port") or {})
                last_runs = dict(state.get("rotation_last_run_by_port") or {})
                countries = dict(state.get("rotation_countries_by_port") or {})
                act_state = dict(state.get("activation_state_by_port") or {})
                current_assign = dict(state["port_ovpn_assignment"])

            due: List[Tuple[int, str, str]] = []  # (port, country_override, current_ovpn)
            for port, mins in intervals.items():
                if mins <= 0:
                    continue
                if act_state.get(port) != "active":
                    continue
                last = last_runs.get(port, 0.0)
                if last <= 0:
                    # Never rotated yet on this active port; seed last_run so the first rotation fires
                    # exactly `mins` minutes from now rather than immediately.
                    with state["lock"]:
                        state.setdefault("rotation_last_run_by_port", {})[port] = now_wall
                    continue
                if (now_wall - last) >= (mins * 60.0):
                    due.append((port, countries.get(port, ""), current_assign.get(port, "")))

            if not due:
                continue

            # Reload runtime config once per tick when something is due.
            runtime_config, load_err, _ = load_disk_config_expanded(config_path)
            if load_err or runtime_config is None:
                _log(f"Rotation tick: could not load config: {load_err}")
                continue
            runtime_config = merge_expanded_locations_from_disk(
                runtime_config, bool(state.get("use_docker"))
            )
            _enforce_default_proxy_auth(runtime_config)
            apply_openvpn_auth_env(runtime_config)
            attach_provider_credentials(runtime_config)

            for port, country_override, current_ovpn in due:
                # Re-check activation under lock right before rotating; skip if user just stopped it.
                with state["lock"]:
                    if (state.get("activation_state_by_port") or {}).get(port) != "active":
                        continue
                chosen, pick_err = _pick_rotation_ovpn(
                    runtime_config,
                    config_path,
                    bool(state.get("use_docker")),
                    country_override,
                    current_ovpn,
                )
                if pick_err or not chosen:
                    _log(f"Rotation skip port {port}: {pick_err or 'no ovpn chosen'}")
                    # Push last_run forward so we do not hammer the loop on every tick when the pool is empty.
                    with state["lock"]:
                        state.setdefault("rotation_last_run_by_port", {})[port] = now_wall
                    continue
                err = _perform_port_rotation_to(
                    state, port, chosen, runtime_config, config_path,
                )
                with state["lock"]:
                    state.setdefault("rotation_last_run_by_port", {})[port] = time.time()
                if err:
                    _log(f"Rotation port {port} -> {chosen}: failed: {err}")
                else:
                    _log(f"Rotation port {port} -> {chosen}")
            persist_assignments_snapshot(state)

            with state["lock"]:
                auth_routes_snapshot = list(state.get("auth_routes") or [])
                auth_route_state = dict(state.get("auth_route_state") or {})
                auth_route_error = dict(state.get("auth_route_error") or {})
                auth_slots = dict(state.get("port_to_slot") or {})
                auth_run = dict(state.get("auth_runtime_config") or {})
                auth_upstreams = dict(state.get("upstream_profiles_by_id") or {})
            auth_due: List[Tuple[str, int, str, str]] = []  # (username, route_index, scheme, current_ovpn)
            for idx, route in enumerate(auth_routes_snapshot):
                egress = dict(route.get("egress") or {})
                if (egress.get("type") or "") != "ovpn":
                    continue
                interval = int(route.get("rotationIntervalMinutes") or 0)
                if interval <= 0 or not bool(route.get("enabled", True)):
                    continue
                scheme = "socks5" if route.get("proxyType") == "socks5" else "http"
                slot_key = _auth_route_backend_key(idx, scheme)
                slot = auth_slots.get(slot_key)
                running = bool(slot and is_backend_running(slot, bool(state.get("use_docker"))))
                status_value = "active" if running else auth_route_state.get(f"{route.get('username')}:{scheme}", "inactive")
                if status_value != "active":
                    continue
                last_run = float(route.get("rotationLastRun") or 0.0)
                if last_run <= 0:
                    with state["lock"]:
                        updated = [dict(r) for r in (state.get("auth_routes") or [])]
                        if idx < len(updated):
                            updated[idx]["rotationLastRun"] = now_wall
                            state["auth_routes"] = updated
                    continue
                if (now_wall - last_run) >= (interval * 60.0):
                    auth_due.append((str(route.get("username") or ""), idx, scheme, (egress.get("ovpn") or "")))

            if auth_due:
                runtime_config, load_err, _ = load_disk_config_expanded(config_path)
                if load_err or runtime_config is None:
                    _log(f"Auth rotation tick: could not load config: {load_err}")
                else:
                    runtime_config = merge_expanded_locations_from_disk(runtime_config, bool(state.get("use_docker")))
                    _enforce_default_proxy_auth(runtime_config)
                    apply_openvpn_auth_env(runtime_config)
                    attach_provider_credentials(runtime_config)
                    for username, route_index, scheme, current_ovpn in auth_due:
                        route = (state.get("auth_routes") or [])[route_index] if route_index < len(state.get("auth_routes") or []) else None
                        if not route or not bool(route.get("enabled", True)):
                            continue
                        rotation_country = (route.get("rotationCountry") or "").strip()
                        chosen, pick_err = _pick_rotation_ovpn(
                            runtime_config,
                            config_path,
                            bool(state.get("use_docker")),
                            rotation_country,
                            current_ovpn,
                        )
                        if pick_err or not chosen:
                            _log(f"Auth rotation skip {username}: {pick_err or 'no ovpn chosen'}")
                            with state["lock"]:
                                updated = [dict(r) for r in (state.get("auth_routes") or [])]
                                if route_index < len(updated):
                                    updated[route_index]["rotationLastRun"] = now_wall
                                    state["auth_routes"] = updated
                            continue
                        err = _perform_auth_route_rotation_to(
                            state,
                            username,
                            chosen,
                            runtime_config,
                            config_path,
                        )
                        with state["lock"]:
                            updated = [dict(r) for r in (state.get("auth_routes") or [])]
                            if route_index < len(updated):
                                updated[route_index]["rotationLastRun"] = time.time()
                                state["auth_routes"] = updated
                        if err:
                            _log(f"Auth rotation {username} -> {chosen}: failed: {err}")
                        else:
                            _log(f"Auth rotation {username} -> {chosen}")
                    persist_assignments_snapshot(state)
        except Exception as e:
            _log(f"Rotation loop error: {e}")


def upstream_refresh_loop(state: Dict[str, Any]) -> None:
    """Restart active upstream-proxy ports on their configured same-profile interval."""
    global shutdown_flag
    config_path = state["config_path"]
    while not shutdown_flag:
        time.sleep(ROTATION_TICK_SECONDS)
        if shutdown_flag:
            break
        try:
            now_wall = time.time()
            due: List[int] = []
            with state["lock"]:
                intervals = dict(state.get("upstream_refresh_intervals_by_port") or {})
                last_runs = dict(state.get("upstream_refresh_last_run_by_port") or {})
                activation = dict(state.get("activation_state_by_port") or {})
                egress = dict(state.get("port_egress_by_port") or {})
            for port, mins in intervals.items():
                if mins <= 0 or activation.get(port) != "active":
                    continue
                if (egress.get(port) or {}).get("type") != "upstream":
                    continue
                last = last_runs.get(port, 0.0)
                if last <= 0:
                    with state["lock"]:
                        state.setdefault("upstream_refresh_last_run_by_port", {})[port] = now_wall
                    continue
                if (now_wall - last) >= (mins * 60.0):
                    due.append(port)
            if not due:
                continue

            runtime_config, load_err, _ = load_disk_config_expanded(config_path)
            if load_err or runtime_config is None:
                _log(f"Upstream refresh tick: could not load config: {load_err}")
                continue
            runtime_config = merge_expanded_locations_from_disk(
                runtime_config,
                bool(state.get("use_docker")),
            )
            _enforce_default_proxy_auth(runtime_config)

            for port in due:
                with state["lock"]:
                    if (state.get("activation_state_by_port") or {}).get(port) != "active":
                        continue
                    current = dict((state.get("port_egress_by_port") or {}).get(port) or {})
                err = _perform_port_egress_change_to(
                    state,
                    port,
                    current,
                    runtime_config,
                    config_path,
                )
                with state["lock"]:
                    state.setdefault("upstream_refresh_last_run_by_port", {})[port] = time.time()
                if err:
                    _log(f"Upstream refresh port {port}: failed: {err}")
                else:
                    _log(f"Upstream refresh port {port}: restarted assigned proxy")
            persist_assignments_snapshot(state)
        except Exception as e:
            _log(f"Upstream refresh loop error: {e}")


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
            elif path == "/api/upstream-proxies":
                self._handle_get_upstream_proxies()
            elif path == "/api/provider-auth":
                self._handle_get_provider_auth()
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
                launcher_ids = dict(state.get("launcher_ids_by_port") or {})
                proxy_types = dict(state.get("proxy_types_by_port") or {})
                rotation_intervals = dict(state.get("rotation_intervals_by_port") or {})
                rotation_countries = dict(state.get("rotation_countries_by_port") or {})
                rotation_last_run = dict(state.get("rotation_last_run_by_port") or {})
                egress_by_port = dict(state.get("port_egress_by_port") or {})
                upstream_profiles = dict(state.get("upstream_profiles_by_id") or {})
                upstream_refresh_intervals = dict(state.get("upstream_refresh_intervals_by_port") or {})
                upstream_refresh_last_run = dict(state.get("upstream_refresh_last_run_by_port") or {})
                active = []
                for port, slot in list(port_to_slot.items()):
                    loc_idx = slot.get("location_index")
                    if loc_idx is None:
                        continue
                    label = locations[loc_idx].get("label", "") if loc_idx < len(locations) else ""
                    last = slot.get("last_activity") or 0
                    age_seconds = max(0.0, now - last) if last else 0.0
                    ptype = (slot.get("proxy_type") or proxy_types.get(port) or "http")
                    if ptype not in ("http", "socks5"):
                        ptype = "http"
                    entry = {
                        "port": port,
                        "locationIndex": loc_idx,
                        "locationLabel": label,
                        "lastActivityAgeSeconds": round(age_seconds, 1),
                        "proxyType": ptype,
                        "egress": _public_egress(egress_by_port.get(port), upstream_profiles),
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
                public_egress_by_port = {
                    str(p): _public_egress(egress_by_port.get(p), upstream_profiles)
                    for p in range(port_base, port_base + num_ports)
                }
            port_max = port_base + num_ports - 1
            listen_h = state.get("listen_host", "127.0.0.1") or "127.0.0.1"
            randomize_country = "random"
            randomize_country_pool = "any country"
            cfg_client = ""
            auto_detect_wan = True
            _cfg, _cfg_err, _cfg_status = load_disk_config_expanded(state["config_path"])
            if _cfg is not None:
                try:
                    randomize_country = normalize_randomize_country(_cfg.get("randomizeCountry"))
                    randomize_country_pool = randomize_country_status_label(_cfg.get("randomizeCountry"))
                    cfg_client = (str(_cfg.get("clientProxyHost") or "")).strip()
                    if "autoDetectClientProxyHost" in _cfg:
                        auto_detect_wan = bool(_cfg.get("autoDetectClientProxyHost"))
                except (TypeError, ValueError):
                    pass
            else:
                try:
                    _log(f"Status config load failed: {_cfg_err or _cfg_status}")
                except Exception:
                    pass
            host_resolution = resolve_client_proxy_host(cfg_client, listen_h, auto_detect_wan)
            client_proxy_host = host_resolution["host"]
            client_proxy_host_source = host_resolution["source"]
            public_wan_ip = host_resolution["publicWanIp"]
            local_auth_routing = bool(state.get("local_auth_routing"))
            copy_host_payload = auth_route_copy_host_payload(cfg_client, local_auth_routing)
            auth_payload: Dict[str, Any] = {"enabled": False}
            if state.get("auth_routing"):
                auth_routes_public: List[Dict[str, Any]] = []
                with lock:
                    route_state = dict(state.get("auth_route_state") or {})
                    route_error = dict(state.get("auth_route_error") or {})
                    route_egress_ip = dict(state.get("auth_route_egress_ip") or {})
                    slots_by_key = dict(state.get("port_to_slot") or {})
                    routes_snapshot = list(state.get("auth_routes") or [])
                    upstream_profiles_snapshot = dict(state.get("upstream_profiles_by_id") or {})
                for idx, route in enumerate(routes_snapshot):
                    protocol_state: Dict[str, Any] = {}
                    route_proxy_type = "socks5" if route.get("proxyType") == "socks5" else "http"
                    for scheme in ("http", "socks5"):
                        if scheme != route_proxy_type:
                            protocol_state[scheme] = {
                                "status": "disabled",
                                "running": False,
                                "lastActivityAgeSeconds": 0,
                                "error": "",
                                "containerName": "",
                            }
                            continue
                        slot_key = _auth_route_backend_key(idx, scheme)
                        slot = slots_by_key.get(slot_key)
                        running = bool(slot and is_backend_running(slot, bool(state.get("use_docker"))))
                        container_name = slot.get("container_name") if slot else ""
                        refresh_slot = slot
                        if bool(state.get("use_docker")) and not running:
                            docker_state = _auth_route_docker_container_state(slot_key)
                            if docker_state.get("running"):
                                running = True
                                container_name = docker_state.get("name") or _auth_route_container_name(slot_key)
                                refresh_slot = {
                                    "backend_host": container_name,
                                    "backend_port": BACKEND_HTTP_PORT,
                                }
                        status_value = "active" if running else route_state.get(f"{route.get('username')}:{scheme}", "inactive")
                        if not running and status_value == "active":
                            status_value = "inactive"
                        last = slot.get("last_activity") if slot else 0
                        age_seconds = max(0.0, now - last) if last else 0.0
                        protocol_key = _auth_route_protocol_key(route.get("username") or "", scheme)
                        egress_ip_info = dict(route_egress_ip.get(protocol_key) or {})
                        if running:
                            _maybe_start_auth_route_egress_ip_refresh(
                                state,
                                route.get("username") or "",
                                scheme,
                                refresh_slot,
                            )
                        protocol_state[scheme] = {
                            "status": status_value,
                            "running": running,
                            "lastActivityAgeSeconds": round(age_seconds, 1),
                            "error": route_error.get(f"{route.get('username')}:{scheme}", ""),
                            "containerName": container_name,
                            "egressPublicIp": egress_ip_info.get("ip") or "",
                            "egressPublicIpCheckedAt": egress_ip_info.get("checkedAt") or None,
                            "egressPublicIpError": egress_ip_info.get("error") or "",
                        }
                    auth_routes_public.append(
                        {
                            "username": route.get("username") or "",
                            "label": route.get("label") or route.get("username") or "",
                            "externalId": route.get("externalId") or route.get("external_id") or "",
                            "proxyType": route_proxy_type,
                            "rotationIntervalMinutes": int(route.get("rotationIntervalMinutes") or 0),
                            "rotationCountry": route.get("rotationCountry") or "",
                            "rotationLastRun": float(route.get("rotationLastRun") or 0.0),
                            "nextRotationAt": (
                                float(route.get("rotationLastRun") or 0.0)
                                + float(route.get("rotationIntervalMinutes") or 0) * 60.0
                                if int(route.get("rotationIntervalMinutes") or 0) > 0
                                and float(route.get("rotationLastRun") or 0.0) > 0
                                else None
                            ),
                            "enabled": bool(route.get("enabled", True)),
                            "egress": _public_egress(route.get("egress"), upstream_profiles_snapshot),
                            "protocols": protocol_state,
                        }
                    )
                auth_payload = {
                    "enabled": True,
                    "httpPort": state.get("auth_http_port"),
                    "socksPort": state.get("auth_socks_port"),
                    "clientProxyHost": client_proxy_host,
                    "clientProxyHostSource": client_proxy_host_source,
                    "localAuthRouting": local_auth_routing,
                    **copy_host_payload,
                    "globalPassword": state.get("auth_global_password") or "",
                    "routes": auth_routes_public,
                }
            self._send_json({
                "running": True,
                "authRouting": auth_payload,
                "portBase": state["port_base"],
                "publishedPortBase": state.get("published_port_base"),
                "dockerPublishedHostPortFirst": state.get("docker_published_host_port_first"),
                "dockerPublishedHostPortLast": state.get("docker_published_host_port_last"),
                "dockerPublishedPortSpan": state.get("docker_published_port_span"),
                "dockerPublishedContainerPortFirst": state.get("docker_published_container_port_first"),
                "dockerPublishedContainerPortLast": state.get("docker_published_container_port_last"),
                "dockerPublishedContainerPortSpan": state.get("docker_published_container_port_span"),
                "publishMismatch": bool(state.get("publish_mismatch")),
                "publishMismatchHint": state.get("publish_mismatch_hint") or "",
                "maxSlots": state["max_slots"],
                "idleTimeoutMinutes": state["idle_timeout_minutes"],
                "useDocker": state["use_docker"],
                "listenHost": listen_h,
                "clientProxyHost": client_proxy_host,
                "clientProxyHostSource": client_proxy_host_source,
                "publicWanIp": public_wan_ip,
                "proxyUsername": state.get("proxy_username") or "",
                "proxyPassword": state.get("proxy_password") or "",
                "controlPort": state.get("control_port", 0),
                "totalPorts": num_ports,
                "portMax": port_max,
                "enabledPorts": enabled_ports,
                "assignedOvpnByPort": assigned_by_port,
                "egressByPort": public_egress_by_port,
                "activationStateByPort": activation_state,
                "activationErrorByPort": activation_error,
                "locations": [
                    {
                        "label": loc.get("label", ""),
                        "ovpn": loc.get("ovpn", ""),
                        "randomAccess": bool(loc.get("randomAccess")),
                        "launcherId": launcher_ids.get(port_base + i, ""),
                        "proxyType": (
                            "socks5"
                            if proxy_types.get(port_base + i) == "socks5"
                            else "http"
                        ),
                        "egress": public_egress_by_port.get(str(port_base + i), {"type": "none"}),
                        "rotationIntervalMinutes": int(rotation_intervals.get(port_base + i, 0)),
                        "rotationCountry": rotation_countries.get(port_base + i, ""),
                        "nextRotationAt": (
                            float(rotation_last_run.get(port_base + i, 0.0))
                            + float(rotation_intervals.get(port_base + i, 0)) * 60.0
                            if rotation_intervals.get(port_base + i, 0) > 0
                            and rotation_last_run.get(port_base + i, 0.0) > 0
                            else None
                        ),
                        "upstreamRefreshIntervalMinutes": int(upstream_refresh_intervals.get(port_base + i, 0)),
                        "nextUpstreamRefreshAt": (
                            float(upstream_refresh_last_run.get(port_base + i, 0.0))
                            + float(upstream_refresh_intervals.get(port_base + i, 0)) * 60.0
                            if upstream_refresh_intervals.get(port_base + i, 0) > 0
                            and upstream_refresh_last_run.get(port_base + i, 0.0) > 0
                            else None
                        ),
                    }
                    for i, loc in enumerate(locations)
                ],
                "activeSlots": active,
                "randomizeCountry": randomize_country,
                "randomizeCountryPool": randomize_country_pool,
            })

        def _handle_get_config(self) -> None:
            config_path = state["config_path"]
            config, load_err, load_status = load_disk_config_expanded(config_path)
            if load_err or config is None:
                self._send_error_body(load_err or "Could not load config", load_status)
                return
            self._send_json(config)

        def _handle_get_ovpn_files(self) -> None:
            config_path = state["config_path"]
            runtime_config, load_err, load_status = load_disk_config_expanded(config_path)
            if load_err:
                self._send_error_body(load_err, load_status)
                return
            runtime_config = merge_expanded_locations_from_disk(
                runtime_config, bool(state.get("use_docker"))
            )
            payload = build_ovpn_files_payload(
                runtime_config,
                config_path,
                bool(state.get("use_docker")),
            )
            self._send_json(payload)

        def _handle_get_upstream_proxies(self) -> None:
            with state["lock"]:
                profiles = list(state.get("upstream_profiles") or [])
            self._send_json(
                {
                    "proxies": [public_profile(profile) for profile in profiles],
                    "count": len(profiles),
                }
            )

        def _read_json_body(self, max_bytes: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                return None, "Invalid Content-Length"
            if content_length <= 0 or content_length > max_bytes:
                return None, "Invalid Content-Length"
            try:
                body = self.rfile.read(content_length).decode("utf-8")
                payload = json.loads(body)
            except Exception as e:
                return None, str(e)
            if not isinstance(payload, dict):
                return None, "Request body must be an object"
            return payload, None

        def _read_ovpn_upload_form(self) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                return None, "Invalid Content-Length", 400
            if content_length <= 0:
                return None, "Invalid Content-Length", 400
            if content_length > OVPN_UPLOAD_MAX_BYTES:
                return None, "Upload is too large (max 64 MB)", 413
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("multipart/form-data"):
                return None, "Content-Type must be multipart/form-data", 400
            try:
                body = self.rfile.read(content_length)
                raw_message = (
                    f"Content-Type: {content_type}\r\n"
                    "MIME-Version: 1.0\r\n\r\n"
                ).encode("utf-8") + body
                message = BytesParser(policy=email_policy).parsebytes(raw_message)
            except Exception as e:
                return None, str(e), 400
            if not message.is_multipart():
                return None, "Invalid multipart body", 400

            text_fields: Dict[str, str] = {}
            files: List[Dict[str, Any]] = []
            for part in message.iter_parts():
                if part.get_content_disposition() != "form-data":
                    continue
                name = str(part.get_param("name", header="content-disposition") or "")
                if not name:
                    continue
                filename = part.get_filename()
                payload_bytes = part.get_payload(decode=True) or b""
                if filename is None:
                    if name not in text_fields:
                        charset = part.get_content_charset() or "utf-8"
                        text_fields[name] = payload_bytes.decode(charset, errors="replace")
                    continue
                if name not in ("files", "files[]"):
                    continue
                files.append({"filename": str(filename), "data": payload_bytes})

            overwrite_raw = text_fields.get("overwrite", "").strip().lower()
            payload = {
                "provider": text_fields.get("provider", "").strip(),
                "username": text_fields.get("username", ""),
                "password": text_fields.get("password", ""),
                "overwrite": overwrite_raw in ("1", "true", "yes", "on"),
                "files": files,
            }
            return payload, None, 200

        def _runtime_config_for_apply(self) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
            runtime_config, load_err, load_status = load_disk_config_expanded(state["config_path"])
            if load_err or runtime_config is None:
                return None, load_err or "Could not load config", load_status
            runtime_config = merge_expanded_locations_from_disk(
                runtime_config,
                bool(state.get("use_docker")),
            )
            _enforce_default_proxy_auth(runtime_config)
            apply_openvpn_auth_env(runtime_config)
            attach_provider_credentials(runtime_config)
            return runtime_config, None, 200

        def _restart_ports_using_upstream_profile(self, profile_id: str) -> List[Dict[str, Any]]:
            runtime_config, load_err, _ = self._runtime_config_for_apply()
            if load_err or runtime_config is None:
                return [{"ok": False, "error": load_err or "Could not load config"}]
            with state["lock"]:
                targets = [
                    (port, dict(egress))
                    for port, egress in (state.get("port_egress_by_port") or {}).items()
                    if egress.get("type") == "upstream"
                    and egress.get("upstreamProxyId") == profile_id
                    and state.get("activation_state_by_port", {}).get(port) in ("starting", "active")
                ]
            results: List[Dict[str, Any]] = []
            for port, egress in targets:
                err = _perform_port_egress_change_to(
                    state,
                    port,
                    egress,
                    runtime_config,
                    state["config_path"],
                )
                results.append({"port": port, "ok": not bool(err), "error": err or ""})
            return results

        def _restart_auth_routes_using_upstream_profile(self, profile_id: str) -> List[Dict[str, Any]]:
            if not state.get("auth_routing"):
                return []
            with state["lock"]:
                routes = [dict(route) for route in (state.get("auth_routes") or [])]
                route_state = dict(state.get("auth_route_state") or {})
                targets = []
                for idx, route in enumerate(routes):
                    egress = dict(route.get("egress") or {})
                    if egress.get("type") != "upstream" or egress.get("upstreamProxyId") != profile_id:
                        continue
                    scheme = "socks5" if route.get("proxyType") == "socks5" else "http"
                    if route_state.get(f"{route.get('username')}:{scheme}") in ("starting", "active"):
                        targets.append((idx, route, scheme))
            results: List[Dict[str, Any]] = []
            for route_index, route, scheme in targets:
                username = str(route.get("username") or "")
                stopped = _stop_auth_route_backends(state, username, scheme)
                if not stopped:
                    results.append({"username": username, "scheme": scheme, "ok": False, "error": "Route not found"})
                    continue
                _slot, err = _start_auth_route_backend(state, route_index, scheme)
                results.append({"username": username, "scheme": scheme, "ok": err is None, "error": err or ""})
            return results

        def _handle_post_upstream_proxy(self) -> None:
            payload, body_err = self._read_json_body(64 * 1024)
            if body_err or payload is None:
                self._send_error_body(body_err or "Invalid body", 400)
                return
            raw_id = (str(payload.get("id") or "")).strip()
            with state["lock"]:
                current_profiles = list(state.get("upstream_profiles") or [])
                existing = (state.get("upstream_profiles_by_id") or {}).get(raw_id) if raw_id else None
            try:
                profile = normalize_profile(payload, existing=existing)
            except UpstreamProxyError as e:
                self._send_error_body(str(e), 400)
                return
            if raw_id and existing is None:
                self._send_error_body(f"Upstream proxy profile not found: {raw_id}", 404)
                return

            if existing:
                next_profiles = [profile if row.get("id") == profile["id"] else row for row in current_profiles]
            else:
                next_profiles = current_profiles + [profile]
            try:
                if state.get("db_store") is not None:
                    state["db_store"].save_upstream_profiles(next_profiles)
                else:
                    save_catalog(state["upstream_catalog_path"], next_profiles)
            except (OSError, UpstreamProxyError) as e:
                self._send_error_body(f"Could not save upstream proxy catalog: {e}", 500)
                return
            except Exception as e:
                self._send_error_body(f"Could not save upstream proxy catalog: {e}", 500)
                return
            with state["lock"]:
                state["upstream_profiles"] = next_profiles
                state["upstream_profiles_by_id"] = _catalog_index(next_profiles)
            restart_results = self._restart_ports_using_upstream_profile(profile["id"]) if existing else []
            auth_restart_results = self._restart_auth_routes_using_upstream_profile(profile["id"]) if existing else []
            self._send_json(
                {
                    "ok": True,
                    "proxy": public_profile(profile),
                    "restartedPorts": [r["port"] for r in restart_results if r.get("ok") and "port" in r],
                    "restartedRoutes": [
                        r["username"] for r in auth_restart_results if r.get("ok") and r.get("username")
                    ],
                    "restartResults": restart_results + auth_restart_results,
                }
            )

        def _handle_post_import_upstream_proxies(self) -> None:
            payload, body_err = self._read_json_body(512 * 1024)
            if body_err or payload is None:
                self._send_error_body(body_err or "Invalid body", 400)
                return
            lines = str(payload.get("lines") or "")
            profiles, results = import_proxy_lines(lines)
            if not profiles and not results:
                self._send_error_body("Paste at least one proxy line", 400)
                return
            with state["lock"]:
                current_profiles = list(state.get("upstream_profiles") or [])
            next_profiles = current_profiles + profiles
            if profiles:
                try:
                    if state.get("db_store") is not None:
                        state["db_store"].save_upstream_profiles(next_profiles)
                    else:
                        save_catalog(state["upstream_catalog_path"], next_profiles)
                except (OSError, UpstreamProxyError) as e:
                    self._send_error_body(f"Could not save upstream proxy catalog: {e}", 500)
                    return
                except Exception as e:
                    self._send_error_body(f"Could not save upstream proxy catalog: {e}", 500)
                    return
                with state["lock"]:
                    state["upstream_profiles"] = next_profiles
                    state["upstream_profiles_by_id"] = _catalog_index(next_profiles)
            self._send_json(
                {
                    "ok": all(r.get("ok") for r in results),
                    "imported": len(profiles),
                    "results": results,
                }
            )

        def _handle_post_delete_upstream_proxy(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            profile_id = (params.get("id", [""])[0] or "").strip()
            if not profile_id:
                self._send_error_body("Missing upstream proxy id", 400)
                return
            with state["lock"]:
                profiles = list(state.get("upstream_profiles") or [])
                existing = (state.get("upstream_profiles_by_id") or {}).get(profile_id)
                referenced_ports = sorted(
                    port
                    for port, egress in (state.get("port_egress_by_port") or {}).items()
                    if egress.get("type") == "upstream" and egress.get("upstreamProxyId") == profile_id
                )
            if not existing:
                self._send_error_body("Upstream proxy profile not found", 404)
                return
            if referenced_ports:
                self._send_error_body(
                    "Upstream proxy is still assigned to port(s): " + ", ".join(str(p) for p in referenced_ports),
                    409,
                )
                return
            next_profiles = [row for row in profiles if row.get("id") != profile_id]
            try:
                if state.get("db_store") is not None:
                    state["db_store"].save_upstream_profiles(next_profiles)
                else:
                    save_catalog(state["upstream_catalog_path"], next_profiles)
            except (OSError, UpstreamProxyError) as e:
                self._send_error_body(f"Could not save upstream proxy catalog: {e}", 500)
                return
            except Exception as e:
                self._send_error_body(f"Could not save upstream proxy catalog: {e}", 500)
                return
            with state["lock"]:
                state["upstream_profiles"] = next_profiles
                state["upstream_profiles_by_id"] = _catalog_index(next_profiles)
            self._send_json({"ok": True, "id": profile_id})

        def _handle_post_set_egress(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            raw_port = params.get("port", [""])[0]
            try:
                port = int(raw_port)
            except (TypeError, ValueError):
                self._send_error_body("Invalid port", 400)
                return
            payload, body_err = self._read_json_body(16 * 1024)
            if body_err or payload is None:
                self._send_error_body(body_err or "Invalid body", 400)
                return
            egress_type = (str(payload.get("type") or "")).strip().lower()
            egress: Dict[str, str] = {"type": egress_type}
            if egress_type == "ovpn":
                egress["ovpn"] = (str(payload.get("ovpn") or "")).strip()
            elif egress_type == "upstream":
                egress["upstreamProxyId"] = (str(payload.get("upstreamProxyId") or "")).strip()
            elif egress_type != "none":
                self._send_error_body('type must be "ovpn", "upstream", or "none"', 400)
                return
            runtime_config, load_err, load_status = self._runtime_config_for_apply()
            if load_err or runtime_config is None:
                self._send_error_body(load_err or "Could not load config", load_status)
                return
            err = _perform_port_egress_change_to(
                state,
                port,
                egress,
                runtime_config,
                state["config_path"],
            )
            if err:
                self._send_error_body(err, 400)
                return
            with state["lock"]:
                public = _public_egress(
                    (state.get("port_egress_by_port") or {}).get(port),
                    state.get("upstream_profiles_by_id") or {},
                )
            self._send_json({"ok": True, "port": port, "egress": public})

        def _handle_get_provider_auth(self) -> None:
            config_path = state["config_path"]
            runtime_config, load_err, load_status = load_disk_config_expanded(config_path)
            if load_err:
                self._send_error_body(load_err, load_status)
                return
            rows, err, status_code = _collect_provider_auth_rows(
                runtime_config,
                config_path,
                bool(state.get("use_docker")),
                state["db_store"].load_provider_credentials() if state.get("db_store") is not None else None,
            )
            if err:
                self._send_error_body(err, status_code)
                return
            self._send_json(
                {
                    "providers": rows,
                    "count": len(rows),
                }
            )

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
            connect_host = (
                "127.0.0.1"
                if listen_host in ("0.0.0.0", "::", "[::]")
                else listen_host
            )
            proxy_user = state.get("proxy_username") or ""
            proxy_pass = state.get("proxy_password") or ""
            with state["lock"]:
                ptype = (state.get("proxy_types_by_port") or {}).get(port) or "http"
            if ptype not in ("http", "socks5"):
                ptype = "http"
            try:
                if ptype == "http":
                    if proxy_user and proxy_pass:
                        user_enc = urllib.parse.quote(proxy_user, safe="")
                        pass_enc = urllib.parse.quote(proxy_pass, safe="")
                        proxy_url = f"http://{user_enc}:{pass_enc}@{connect_host}:{port}"
                    else:
                        proxy_url = f"http://{connect_host}:{port}"
                    import urllib.request as urllib_request

                    proxy_handler = urllib_request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                    opener = urllib_request.build_opener(proxy_handler)
                    req = urllib_request.Request(
                        "https://api.ipify.org?format=json",
                        headers={"User-Agent": "OpenVPN-Proxy-Gateway/1.0"},
                    )
                    with opener.open(req, timeout=15) as resp:
                        body = resp.read().decode("utf-8")
                    match = re.search(r'"ip"\s*:\s*"([^"]+)"', body) if body else None
                    exit_ip = match.group(1) if match else body.strip()
                    self._send_json({"ok": True, "exitIp": exit_ip})
                    return
                try:
                    import socks  # type: ignore
                except ImportError:
                    self._send_json(
                        {
                            "ok": False,
                            "error": "SOCKS5 test requires PySocks (pip install PySocks).",
                        },
                        status=500,
                    )
                    return
                s = socks.socksocket()
                s.set_proxy(
                    socks.SOCKS5,
                    connect_host,
                    port,
                    rdns=False,
                    username=proxy_user or None,
                    password=proxy_pass or None,
                )
                s.settimeout(20)
                s.connect(("api.ipify.org", 443))
                ctx = __import__("ssl").create_default_context()
                tls = ctx.wrap_socket(s, server_hostname="api.ipify.org")
                req_line = (
                    b"GET /?format=json HTTP/1.1\r\n"
                    b"Host: api.ipify.org\r\n"
                    b"User-Agent: OpenVPN-Proxy-Gateway/1.0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                tls.sendall(req_line)
                chunks: List[bytes] = []
                while True:
                    chunk = tls.recv(8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
                tls.close()
                raw = b"".join(chunks).decode("utf-8", errors="replace")
                _hdr, _, body = raw.partition("\r\n\r\n")
                match = re.search(r'"ip"\s*:\s*"([^"]+)"', body) if body else None
                exit_ip = match.group(1) if match else body.strip()
                self._send_json({"ok": True, "exitIp": exit_ip})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)

        def _require_auth_routing(self) -> bool:
            if not state.get("auth_routing"):
                self._send_error_body("Auth routing mode is not enabled", 404)
                return False
            return True

        def _auth_route_from_body(self, existing_routes: Iterable[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
            payload, body_err = self._read_json_body(64 * 1024)
            if body_err or payload is None:
                return None, body_err or "Invalid body"
            auto_username = bool(payload.get("autoGenerateUsername"))
            username = _auth_route_unique_username(payload, existing_routes) if auto_username else (str(payload.get("username") or "")).strip()
            if not username:
                return None, "username is required"
            if any(c.isspace() for c in username) or any(c in username for c in ":/@\\\r\n\x00"):
                return None, "username must not contain spaces, URL separators, or control characters"
            external_id = (str(payload.get("externalId") or payload.get("external_id") or "")).strip()
            external_id = re.sub(r"[\r\n\x00]+", " ", external_id)[:256].strip()
            label = (str(payload.get("label") or external_id or username)).strip() or username
            proxy_type = "socks5" if (str(payload.get("proxyType") or payload.get("proxy_type") or "")).strip().lower() == "socks5" else "http"
            try:
                rotation_minutes = int(payload.get("rotationIntervalMinutes") or payload.get("rotation_interval_minutes") or 0)
            except (TypeError, ValueError):
                return None, "rotationIntervalMinutes must be a non-negative integer"
            if rotation_minutes < 0:
                return None, "rotationIntervalMinutes must be a non-negative integer"
            rotation_minutes = min(_ROTATION_INTERVAL_MAX_MINUTES, rotation_minutes)
            rotation_country_raw = (str(payload.get("rotationCountry") or payload.get("rotation_country") or "")).strip()
            rotation_country = ""
            if rotation_minutes > 0 and rotation_country_raw:
                country_norm = normalize_randomize_country(rotation_country_raw)
                if country_norm == "random" and rotation_country_raw.lower() not in ("", "random"):
                    return None, 'rotationCountry must be a 2-letter ISO code or empty for "use global default"'
                if country_norm != "random":
                    rotation_country = country_norm
            try:
                rotation_last_run = float(payload.get("rotationLastRun") or payload.get("rotation_last_run") or 0.0)
            except (TypeError, ValueError):
                rotation_last_run = 0.0
            enabled = payload.get("enabled")
            if enabled is None:
                enabled = True
            egress = _normalize_auth_route_egress(payload)
            if egress.get("type") != "ovpn":
                rotation_minutes = 0
                rotation_country = ""
                rotation_last_run = 0.0
            route = {
                "index": 0,
                "username": username,
                "label": label,
                "externalId": external_id,
                "proxyType": proxy_type,
                "rotationIntervalMinutes": rotation_minutes,
                "rotationCountry": rotation_country,
                "rotationLastRun": rotation_last_run if rotation_minutes > 0 else 0.0,
                "enabled": bool(enabled),
                "egress": egress,
            }
            if egress.get("type") == "ovpn":
                ovpn = (egress.get("ovpn") or "").strip()
                if Path(ovpn).suffix.lower() != ".ovpn" or not _is_safe_relative_ovpn_name(ovpn):
                    return None, "ovpn must be a safe relative .ovpn path"
                allowed = list_allowed_ovpn_files(
                    dict(state.get("auth_runtime_config") or {}),
                    state["config_path"],
                    bool(state.get("use_docker")),
                )
                if ovpn not in allowed:
                    return None, "Selected ovpn is not in allowed list"
            if egress.get("type") == "upstream":
                upstream_id = (egress.get("upstreamProxyId") or "").strip()
                if not upstream_id:
                    return None, "upstreamProxyId is required for upstream routes"
                if upstream_id not in (state.get("upstream_profiles_by_id") or {}):
                    return None, f"Upstream proxy profile not found: {upstream_id}"
            return route, None

        def _handle_post_auth_route(self) -> None:
            if not self._require_auth_routing():
                return
            with state["lock"]:
                routes = [dict(r) for r in (state.get("auth_routes") or [])]
            route, err = self._auth_route_from_body(routes)
            if err or route is None:
                self._send_error_body(err or "Invalid route", 400)
                return
            with state["lock"]:
                routes = [dict(r) for r in (state.get("auth_routes") or [])]
                existing_idx = next(
                    (i for i, r in enumerate(routes) if (r.get("username") or "") == route["username"]),
                    None,
                )
                if existing_idx is None:
                    route["index"] = len(routes)
                    routes.append(route)
                else:
                    route["index"] = existing_idx
                    routes[existing_idx] = route
                for i, r in enumerate(routes):
                    r["index"] = i
            save_err = _persist_auth_routes_config(state["config_path"], state, routes)
            if save_err:
                self._send_error_body(save_err, 500)
                return
            if existing_idx is not None:
                _stop_auth_route_backends(state, route["username"], "both")
            with state["lock"]:
                state["auth_routes"] = routes
                runtime = dict(state.get("auth_runtime_config") or {})
                auth_cfg = dict(_auth_routing_dict(runtime))
                auth_cfg.update(
                    {
                        "enabled": True,
                        "httpPort": state.get("auth_http_port"),
                        "socksPort": state.get("auth_socks_port"),
                        "routes": routes,
                    }
                )
                runtime["authRouting"] = auth_cfg
                state["auth_runtime_config"] = runtime
            self._send_json({"ok": True, "route": route})

        def _auth_route_username_from_query(self, query: str) -> Tuple[str, str]:
            params = urllib.parse.parse_qs(query)
            username = (params.get("username", [""])[0] or "").strip()
            scheme = (params.get("scheme", ["both"])[0] or "both").strip().lower()
            if scheme not in ("http", "socks5", "both"):
                scheme = "both"
            return username, scheme

        def _handle_post_auth_route_start(self, query: str) -> None:
            if not self._require_auth_routing():
                return
            username, scheme = self._auth_route_username_from_query(query)
            if not username:
                self._send_error_body("Missing username", 400)
                return
            found = _auth_route_by_username(state.get("auth_routes") or [], username)
            if not found:
                self._send_error_body("Route not found", 404)
                return
            route_index, route = found
            route_proxy_type = "socks5" if route.get("proxyType") == "socks5" else "http"
            schemes = [route_proxy_type] if scheme == "both" else [scheme]
            results = []
            for item in schemes:
                _slot, err = _start_auth_route_backend(state, route_index, item)
                results.append({"scheme": item, "ok": err is None, "error": err or ""})
            ok = all(r["ok"] for r in results)
            self._send_json({"ok": ok, "username": username, "results": results}, status=200 if ok else 400)

        def _handle_post_auth_route_stop(self, query: str) -> None:
            if not self._require_auth_routing():
                return
            username, scheme = self._auth_route_username_from_query(query)
            if not username:
                self._send_error_body("Missing username", 400)
                return
            if not _stop_auth_route_backends(state, username, scheme):
                self._send_error_body("Route not found", 404)
                return
            self._send_json({"ok": True, "username": username})

        def _handle_post_auth_route_restart(self, query: str) -> None:
            if not self._require_auth_routing():
                return
            username, scheme = self._auth_route_username_from_query(query)
            if not username:
                self._send_error_body("Missing username", 400)
                return
            found = _auth_route_by_username(state.get("auth_routes") or [], username)
            if not found:
                self._send_error_body("Route not found", 404)
                return
            route_index, route = found
            route_proxy_type = "socks5" if route.get("proxyType") == "socks5" else "http"
            schemes = [route_proxy_type] if scheme == "both" else [scheme]
            if not _stop_auth_route_backends(state, username, scheme):
                self._send_error_body("Route not found", 404)
                return
            refresh_upstream_session = (route.get("egress") or {}).get("type") == "upstream"
            results = []
            for item in schemes:
                _slot, err = _start_auth_route_backend(
                    state,
                    route_index,
                    item,
                    refresh_upstream_session=refresh_upstream_session,
                )
                results.append({"scheme": item, "ok": err is None, "error": err or ""})
            ok = all(r["ok"] for r in results)
            self._send_json({"ok": ok, "username": username, "results": results}, status=200 if ok else 400)

        def _handle_post_auth_route_delete(self, query: str) -> None:
            if not self._require_auth_routing():
                return
            username, _scheme = self._auth_route_username_from_query(query)
            if not username:
                self._send_error_body("Missing username", 400)
                return
            if not _stop_auth_route_backends(state, username, "both"):
                self._send_error_body("Route not found", 404)
                return
            with state["lock"]:
                routes = [
                    dict(r)
                    for r in (state.get("auth_routes") or [])
                    if (r.get("username") or "") != username
                ]
                for i, r in enumerate(routes):
                    r["index"] = i
            save_err = _persist_auth_routes_config(state["config_path"], state, routes)
            if save_err:
                self._send_error_body(save_err, 500)
                return
            with state["lock"]:
                state["auth_routes"] = routes
            self._send_json({"ok": True, "username": username})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/")
            if path == "/api/config":
                self._handle_post_config()
            elif path == "/api/provider-auth":
                self._handle_post_provider_auth()
            elif path == "/api/ovpn-upload":
                self._handle_post_ovpn_upload()
            elif path == "/api/assign-ovpn":
                self._handle_post_assign_ovpn(parsed.query)
            elif path == "/api/upstream-proxy":
                self._handle_post_upstream_proxy()
            elif path == "/api/import-upstream-proxies":
                self._handle_post_import_upstream_proxies()
            elif path == "/api/delete-upstream-proxy":
                self._handle_post_delete_upstream_proxy(parsed.query)
            elif path == "/api/set-egress":
                self._handle_post_set_egress(parsed.query)
            elif path == "/api/change-port-location":
                self._handle_post_change_port_location(parsed.query)
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
            elif path == "/api/set-launcher-id":
                self._handle_post_set_launcher_id(parsed.query)
            elif path == "/api/set-proxy-type":
                self._handle_post_set_proxy_type(parsed.query)
            elif path == "/api/set-rotation":
                self._handle_post_set_rotation(parsed.query)
            elif path == "/api/set-upstream-refresh":
                self._handle_post_set_upstream_refresh(parsed.query)
            elif path == "/api/auth-route":
                self._handle_post_auth_route()
            elif path == "/api/auth-route-start":
                self._handle_post_auth_route_start(parsed.query)
            elif path == "/api/auth-route-stop":
                self._handle_post_auth_route_stop(parsed.query)
            elif path == "/api/auth-route-restart":
                self._handle_post_auth_route_restart(parsed.query)
            elif path == "/api/auth-route-delete":
                self._handle_post_auth_route_delete(parsed.query)
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
            if state.get("db_store") is not None:
                try:
                    state["db_store"].save_config(to_save)
                except Exception as e:
                    self._send_error_body(f"Could not save config to database: {e}", 500)
                    return
                self._send_json({"ok": True, "message": "Config saved. Restart the gateway to apply."})
                return
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

        def _handle_post_provider_auth(self) -> None:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0 or content_length > 256 * 1024:
                self._send_error_body("Invalid Content-Length", 400)
                return
            try:
                body = self.rfile.read(content_length).decode("utf-8")
                payload = json.loads(body)
            except Exception as e:
                self._send_error_body(str(e), 400)
                return
            entries = payload.get("providers") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                self._send_error_body("providers must be an array", 400)
                return

            config_path = state["config_path"]
            runtime_config, load_err, load_status = load_disk_config_expanded(config_path)
            if load_err:
                self._send_error_body(load_err, load_status)
                return
            ovpn_root = _resolve_provider_auth_root(
                runtime_config,
                config_path,
                bool(state.get("use_docker")),
            )
            if not ovpn_root.exists() or not ovpn_root.is_dir():
                self._send_error_body(f"ovpnRoot does not exist or is not a directory: {ovpn_root}", 400)
                return

            if state.get("db_store") is not None:
                results: List[Dict[str, Any]] = []
                had_error = False
                rows_to_save: List[Dict[str, str]] = []
                for i, item in enumerate(entries):
                    if not isinstance(item, dict):
                        results.append({"index": i, "ok": False, "error": "Entry must be an object"})
                        had_error = True
                        continue
                    provider = (str(item.get("provider") or "")).strip()
                    username = str(item.get("username") or "")
                    password = str(item.get("password") or "")
                    if not _is_safe_provider_name(provider):
                        results.append({"provider": provider, "ok": False, "error": "provider must be a safe single folder name"})
                        had_error = True
                        continue
                    if any(c in username for c in "\r\n\x00"):
                        results.append({"provider": provider, "ok": False, "error": "username contains invalid characters"})
                        had_error = True
                        continue
                    if any(c in password for c in "\r\n\x00"):
                        results.append({"provider": provider, "ok": False, "error": "password contains invalid characters"})
                        had_error = True
                        continue
                    provider_dir = (ovpn_root / provider).resolve()
                    if not provider_dir.is_dir():
                        results.append({"provider": provider, "ok": False, "error": "Provider directory not found under ovpnRoot"})
                        had_error = True
                        continue
                    if not _is_safe_under_root(provider_dir, ovpn_root):
                        results.append({"provider": provider, "ok": False, "error": "Unsafe provider path"})
                        had_error = True
                        continue
                    rows_to_save.append({"provider": provider, "username": username.strip(), "password": password})
                    results.append({"provider": provider, "ok": True, "authPath": "", "storedIn": "postgres"})
                if not had_error:
                    try:
                        state["db_store"].save_provider_credentials(rows_to_save)
                    except Exception as e:
                        self._send_error_body(f"Could not save provider credentials to database: {e}", 500)
                        return
                status_code = 200 if not had_error else 400
                self._send_json({"ok": not had_error, "results": results}, status=status_code)
                return

            results: List[Dict[str, Any]] = []
            had_error = False
            for i, item in enumerate(entries):
                if not isinstance(item, dict):
                    results.append({"index": i, "ok": False, "error": "Entry must be an object"})
                    had_error = True
                    continue
                provider = (str(item.get("provider") or "")).strip()
                username = str(item.get("username") or "")
                password = str(item.get("password") or "")
                if not _is_safe_provider_name(provider):
                    results.append(
                        {
                            "provider": provider,
                            "ok": False,
                            "error": "provider must be a safe single folder name",
                        }
                    )
                    had_error = True
                    continue
                if any(c in username for c in "\r\n\x00"):
                    results.append({"provider": provider, "ok": False, "error": "username contains invalid characters"})
                    had_error = True
                    continue
                if any(c in password for c in "\r\n\x00"):
                    results.append({"provider": provider, "ok": False, "error": "password contains invalid characters"})
                    had_error = True
                    continue
                provider_dir = (ovpn_root / provider).resolve()
                auth_path = (provider_dir / "auth.txt").resolve()
                if not provider_dir.is_dir():
                    results.append({"provider": provider, "ok": False, "error": "Provider directory not found under ovpnRoot"})
                    had_error = True
                    continue
                if not _is_safe_under_root(provider_dir, ovpn_root) or not _is_safe_under_root(auth_path, ovpn_root):
                    results.append({"provider": provider, "ok": False, "error": "Unsafe provider path"})
                    had_error = True
                    continue
                try:
                    auth_path.write_text(f"{username.strip()}\n{password}\n", encoding="utf-8")
                    results.append({"provider": provider, "ok": True, "authPath": str(auth_path)})
                except OSError as e:
                    had_error = True
                    if getattr(e, "errno", None) == errno.EROFS or "read-only" in str(e).lower():
                        results.append(
                            {
                                "provider": provider,
                                "ok": False,
                                "error": (
                                    "OVPN mount is read-only. In Docker, change gateway volume "
                                    "to `ovpn_data:/ovpn`, then recreate "
                                    "the stack (`docker compose down && docker compose up -d`)."
                                ),
                            }
                        )
                    else:
                        results.append({"provider": provider, "ok": False, "error": str(e)})

            status_code = 200 if not had_error else 400
            self._send_json({"ok": not had_error, "results": results}, status=status_code)

        def _handle_post_ovpn_upload(self) -> None:
            payload, form_err, status_code = self._read_ovpn_upload_form()
            if form_err or payload is None:
                self._send_error_body(form_err or "Invalid upload", status_code)
                return
            runtime_config, load_err, load_status = self._runtime_config_for_apply()
            if load_err or runtime_config is None:
                self._send_error_body(load_err or "Could not load config", load_status)
                return
            ovpn_root = _resolve_provider_auth_root(
                runtime_config,
                state["config_path"],
                bool(state.get("use_docker")),
            )
            try:
                result = save_ovpn_upload_batch(
                    ovpn_root,
                    str(payload.get("provider") or ""),
                    str(payload.get("username") or ""),
                    str(payload.get("password") or ""),
                    list(payload.get("files") or []),
                    bool(payload.get("overwrite")),
                    write_auth_file=state.get("db_store") is None,
                )
                if state.get("db_store") is not None:
                    upload_user = str(payload.get("username") or "").strip()
                    upload_pass = str(payload.get("password") or "")
                    if upload_user and upload_pass:
                        state["db_store"].upsert_provider_credential(
                            str(payload.get("provider") or ""),
                            upload_user,
                            upload_pass,
                        )
            except OvpnUploadError as e:
                self._send_error_body(str(e), 400)
                return
            except Exception as e:
                self._send_error_body(f"Could not save provider credentials to database: {e}", 500)
                return
            files_payload = build_ovpn_files_payload(
                runtime_config,
                state["config_path"],
                bool(state.get("use_docker")),
            )
            result["ovpnCount"] = files_payload.get("ovpnCount", 0)
            result["providers"] = files_payload.get("providers", [])
            self._send_json(result)

        def _handle_post_change_port_location(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            raw_port = params.get("port", [""])[0]
            try:
                port = int(raw_port)
            except (TypeError, ValueError):
                self._send_error_body("Invalid port", 400)
                return
            payload, body_err = self._read_json_body(16 * 1024)
            if body_err or payload is None:
                self._send_error_body(body_err or "Invalid body", 400)
                return
            runtime_config, load_err, load_status = self._runtime_config_for_apply()
            if load_err or runtime_config is None:
                self._send_error_body(load_err or "Could not load config", load_status)
                return
            result, err = _perform_port_location_change(
                state,
                port,
                runtime_config,
                state["config_path"],
                requested_ovpn=str(payload.get("ovpn") or ""),
                requested_country=str(payload.get("country") or ""),
            )
            if err or result is None:
                self._send_error_body(err or "Failed to change location", 400)
                return
            self._send_json(result)

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
                    if (state.get("port_egress_by_port") or {}).get(port, {}).get("type") == "ovpn":
                        state.setdefault("port_egress_by_port", {}).pop(port, None)
                    if (state.get("rotation_intervals_by_port") or {}).get(port, 0) > 0:
                        state.setdefault("rotation_last_run_by_port", {})[port] = time.time()
                persist_assignments_snapshot(state)
                self._send_json({"ok": True, "port": port, "ovpn": ""})
                return

            if Path(ovpn).suffix.lower() != ".ovpn":
                self._send_error_body("Only .ovpn files are allowed", 400)
                return
            if not _is_safe_relative_ovpn_name(ovpn):
                self._send_error_body("ovpn must be a safe relative path", 400)
                return
            runtime_config, load_err, load_status = load_disk_config_expanded(state["config_path"])
            if load_err or runtime_config is None:
                self._send_error_body(load_err or "Could not read config", load_status)
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
                state.setdefault("port_egress_by_port", {})[port] = {"type": "ovpn", "ovpn": ovpn}
                if (state.get("rotation_intervals_by_port") or {}).get(port, 0) > 0:
                    state.setdefault("rotation_last_run_by_port", {})[port] = time.time()
            persist_assignments_snapshot(state)
            self._send_json({"ok": True, "port": port, "ovpn": ovpn})

        def _handle_post_set_launcher_id(self, query: str) -> None:
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
            if content_length < 0 or content_length > 4096:
                self._send_error_body("Invalid Content-Length", 400)
                return
            try:
                body = (self.rfile.read(content_length).decode("utf-8") if content_length else "{}")
                payload = json.loads(body)
            except Exception as e:
                self._send_error_body(str(e), 400)
                return
            raw = payload.get("launcherId")
            s = (str(raw) if raw is not None else "").strip()
            if len(s) > 256:
                s = s[:256]
            if any(c in s for c in "\r\n\t\x00"):
                self._send_error_body("launcherId contains invalid characters", 400)
                return
            port_base = state["port_base"]
            num_ports = state.get("num_ports") or len(state["locations"])
            if port < port_base or port >= port_base + num_ports:
                self._send_error_body("Port out of location range", 400)
                return
            with state["lock"]:
                lids = state.setdefault("launcher_ids_by_port", {})
                if not s:
                    lids.pop(port, None)
                else:
                    lids[port] = s
            persist_assignments_snapshot(state)
            self._send_json({"ok": True, "port": port, "launcherId": s})

        def _handle_post_set_proxy_type(self, query: str) -> None:
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
            if content_length < 0 or content_length > 4096:
                self._send_error_body("Invalid Content-Length", 400)
                return
            try:
                body = (self.rfile.read(content_length).decode("utf-8") if content_length else "{}")
                payload = json.loads(body)
            except Exception as e:
                self._send_error_body(str(e), 400)
                return
            raw_type = payload.get("proxyType")
            scheme = (str(raw_type) if raw_type is not None else "").strip().lower()
            if scheme not in ("http", "socks5"):
                self._send_error_body('proxyType must be "http" or "socks5"', 400)
                return
            port_base = state["port_base"]
            num_ports = state.get("num_ports") or len(state["locations"])
            if port < port_base or port >= port_base + num_ports:
                self._send_error_body("Port out of location range", 400)
                return
            lock = state["lock"]
            with lock:
                pt = state.setdefault("proxy_types_by_port", {})
                if scheme == "http":
                    pt.pop(port, None)
                else:
                    pt[port] = "socks5"
                _deactivate_listener_port_unlocked(state, port)
            persist_assignments_snapshot(state)
            self._send_json({"ok": True, "port": port, "proxyType": scheme})

        def _handle_post_set_rotation(self, query: str) -> None:
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
            if content_length < 0 or content_length > 4096:
                self._send_error_body("Invalid Content-Length", 400)
                return
            try:
                body = (self.rfile.read(content_length).decode("utf-8") if content_length else "{}")
                payload = json.loads(body)
            except Exception as e:
                self._send_error_body(str(e), 400)
                return
            raw_interval = payload.get("intervalMinutes")
            try:
                interval_minutes = int(raw_interval) if raw_interval is not None else 0
            except (TypeError, ValueError):
                self._send_error_body("intervalMinutes must be a non-negative integer", 400)
                return
            if interval_minutes < 0:
                self._send_error_body("intervalMinutes must be a non-negative integer", 400)
                return
            if interval_minutes > _ROTATION_INTERVAL_MAX_MINUTES:
                interval_minutes = _ROTATION_INTERVAL_MAX_MINUTES
            raw_country = payload.get("country")
            country_str = (str(raw_country) if raw_country is not None else "").strip()
            country_norm = normalize_randomize_country(country_str) if country_str else "random"
            if country_str and country_norm == "random" and country_str.lower() not in ("", "random"):
                self._send_error_body(
                    'country must be a 2-letter ISO code or empty for "use global default"',
                    400,
                )
                return
            port_base = state["port_base"]
            num_ports = state.get("num_ports") or len(state["locations"])
            if port < port_base or port >= port_base + num_ports:
                self._send_error_body("Port out of location range", 400)
                return
            with state["lock"]:
                current_egress = dict((state.get("port_egress_by_port") or {}).get(port) or {})
                if interval_minutes > 0 and current_egress.get("type") == "upstream":
                    self._send_error_body("OVPN rotation is not available for upstream proxy egress", 400)
                    return
                ri = state.setdefault("rotation_intervals_by_port", {})
                rc = state.setdefault("rotation_countries_by_port", {})
                rl = state.setdefault("rotation_last_run_by_port", {})
                if interval_minutes == 0:
                    ri.pop(port, None)
                    rc.pop(port, None)
                    rl.pop(port, None)
                else:
                    ri[port] = interval_minutes
                    if country_norm == "random":
                        rc.pop(port, None)
                    else:
                        rc[port] = country_norm
                    # Reset timer so the next rotation fires `interval` minutes from save.
                    rl[port] = time.time()
            persist_assignments_snapshot(state)
            self._send_json(
                {
                    "ok": True,
                    "port": port,
                    "intervalMinutes": interval_minutes,
                    "country": "" if country_norm == "random" else country_norm,
                }
            )

        def _handle_post_set_upstream_refresh(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            try:
                port = int(params.get("port", [""])[0])
            except (TypeError, ValueError):
                self._send_error_body("Invalid port", 400)
                return
            payload, body_err = self._read_json_body(4096)
            if body_err or payload is None:
                self._send_error_body(body_err or "Invalid body", 400)
                return
            try:
                interval_minutes = int(payload.get("intervalMinutes") or 0)
            except (TypeError, ValueError):
                self._send_error_body("intervalMinutes must be a non-negative integer", 400)
                return
            if interval_minutes < 0:
                self._send_error_body("intervalMinutes must be a non-negative integer", 400)
                return
            interval_minutes = min(interval_minutes, _ROTATION_INTERVAL_MAX_MINUTES)
            port_base = state["port_base"]
            num_ports = state.get("num_ports") or len(state["locations"])
            if port < port_base or port >= port_base + num_ports:
                self._send_error_body("Port out of location range", 400)
                return
            with state["lock"]:
                egress = dict((state.get("port_egress_by_port") or {}).get(port) or {})
                if interval_minutes > 0 and egress.get("type") != "upstream":
                    self._send_error_body("Upstream refresh requires an upstream proxy egress", 400)
                    return
                ri = state.setdefault("upstream_refresh_intervals_by_port", {})
                rl = state.setdefault("upstream_refresh_last_run_by_port", {})
                if interval_minutes == 0:
                    ri.pop(port, None)
                    rl.pop(port, None)
                else:
                    ri[port] = interval_minutes
                    rl[port] = time.time()
            persist_assignments_snapshot(state)
            self._send_json({"ok": True, "port": port, "intervalMinutes": interval_minutes})

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
            runtime_config = merge_expanded_locations_from_disk(
                runtime_config, bool(state.get("use_docker"))
            )
            _enforce_default_proxy_auth(runtime_config)
            apply_openvpn_auth_env(runtime_config)
            attach_provider_credentials(runtime_config)
            assigned_ovpn = ""
            egress: Dict[str, str] = {}
            rotation_minutes = 0
            rotation_country = ""
            with state["lock"]:
                assigned_ovpn = (state["port_ovpn_assignment"].get(port) or "").strip()
                egress = dict((state.get("port_egress_by_port") or {}).get(port) or {})
                rotation_minutes = int((state.get("rotation_intervals_by_port") or {}).get(port, 0) or 0)
                rotation_country = (state.get("rotation_countries_by_port") or {}).get(port, "")
            if (not egress or egress.get("type") == "ovpn") and not assigned_ovpn and rotation_minutes > 0:
                chosen, pick_err = _pick_rotation_ovpn(
                    runtime_config,
                    config_path,
                    bool(state.get("use_docker")),
                    rotation_country,
                    "",
                )
                if pick_err or not chosen:
                    self._send_error_body(
                        pick_err or "No .ovpn files available to rotate",
                        400,
                    )
                    return
                with state["lock"]:
                    state["port_ovpn_assignment"][port] = chosen
                    state.setdefault("port_egress_by_port", {})[port] = {"type": "ovpn", "ovpn": chosen}
                    # Activation starts from now; do not trigger immediate rotation.
                    state.setdefault("rotation_last_run_by_port", {})[port] = time.time()
                persist_assignments_snapshot(state)
                assigned_ovpn = chosen
                egress = {"type": "ovpn", "ovpn": chosen}
            if not egress:
                self._send_error_body("Select an OVPN profile or upstream proxy for this port before activation", 400)
                return
            err = validate_port_egress(
                runtime_config,
                config_path,
                loc_idx,
                bool(state.get("use_docker")),
                egress,
                state.get("upstream_profiles_by_id") or {},
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
                # Reset the rotation timer so a stale timestamp from a previous run does not trigger
                # an immediate rotation right after the user reactivates this port.
                if (state.get("rotation_intervals_by_port") or {}).get(port, 0) > 0:
                    state.setdefault("rotation_last_run_by_port", {})[port] = time.time()
                if (state.get("upstream_refresh_intervals_by_port") or {}).get(port, 0) > 0:
                    state.setdefault("upstream_refresh_last_run_by_port", {})[port] = time.time()

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

            deactivate_listener_port(state, port)
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
            runtime_config = merge_expanded_locations_from_disk(
                runtime_config, bool(state.get("use_docker"))
            )
            _enforce_default_proxy_auth(runtime_config)
            apply_openvpn_auth_env(runtime_config)
            attach_provider_credentials(runtime_config)

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

            err = _perform_port_rotation_to(
                state, port, chosen, runtime_config, config_path,
            )
            if err:
                self._send_error_body(err, 400)
                return
            # Manual randomize counts as a rotation event — reset timer so auto-rotation starts fresh.
            with lock:
                state.setdefault("rotation_last_run_by_port", {})[port] = time.time()
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
            runtime_config = merge_expanded_locations_from_disk(
                runtime_config, bool(state.get("use_docker"))
            )
            _enforce_default_proxy_auth(runtime_config)
            apply_openvpn_auth_env(runtime_config)
            attach_provider_credentials(runtime_config)

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
    global DB_STORE
    _request_admin_rerun()
    parser = argparse.ArgumentParser(description="Dynamic proxy gateway (on-demand, idle shutdown).")
    parser.add_argument("--config", default=str(script_dir() / "openvpn-proxy-config.json"), help="Path to config JSON")
    args = parser.parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = script_dir() / args.config
    if storage_enabled():
        try:
            DB_STORE = PorticoStore(storage_database_url())
            DB_STORE.initialize(
                config_path if config_path.is_file() else None,
                resolve_assignments_path(config_path),
                resolve_upstream_catalog_path(config_path),
            )
            config = DB_STORE.load_config()
            _log("Config store: Postgres")
        except Exception as e:
            print(f"Could not initialize database store: {e}", file=sys.stderr)
            return 1
    else:
        if not config_path.exists():
            print(f"Config not found: {config_path}", file=sys.stderr)
            return 1
        if config_path.is_dir():
            print(f"Config path is a directory, not a file: {config_path}.", file=sys.stderr)
            return 1

        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, UnicodeDecodeError) as e:
            print(f"Could not read config file: {e}", file=sys.stderr)
            return 1
        except json.JSONDecodeError as e:
            print(f"Invalid JSON in config file {config_path}: {e}", file=sys.stderr)
            return 1
    try:
        apply_location_spec(config)
    except ValueError as e:
        print(f"Invalid locationSpec: {e}", file=sys.stderr)
        return 1
    use_docker = config.get("useDocker") is True or os.environ.get("USE_DOCKER", "").lower() in ("1", "true", "yes")
    auth_routing = is_auth_routing_enabled(config)
    local_auth_routing = _env_truthy("AUTH_ROUTING_ENABLED")
    if auth_routing and DB_STORE is not None:
        auth_routes = DB_STORE.load_auth_routes()
        auth_cfg = dict(_auth_routing_dict(config))
        auth_cfg["routes"] = auth_routes
        config["authRouting"] = auth_cfg
    else:
        auth_routes = normalize_auth_routes(config) if auth_routing else []

    locations_raw = list(config.get("locations") or [])
    _log(f"Config loaded: {len(locations_raw)} location row(s) from {config_path}")
    locations = [] if auth_routing else apply_docker_published_listener_slots(locations_raw, config, use_docker)
    config["locations"] = locations
    if auth_routing:
        _log(f"Auth routing mode enabled: {len(auth_routes)} route(s)")
    elif locations_raw and len(locations) != len(locations_raw):
        _log(
            f"Effective TCP listeners: {len(locations)} (Docker DOCKER_PROXY_CONTAINER_PORT_FIRST/LAST publish span)."
        )

    _enforce_default_proxy_auth(config)
    apply_openvpn_auth_env(config)
    attach_provider_credentials(config)

    if not locations and not auth_routing:
        _log(
            "No locations in config (add a locations[] array or a valid locationSpec). "
            "Control API will start so the dashboard can load; add locations and restart the gateway for proxy listeners."
        )
    if auth_routing and not auth_routes:
        _log("Auth routing is enabled but no authRouting.routes are configured; proxy auth will reject all routes.")

    docker_image = config.get("dockerImage") or os.environ.get("DOCKER_IMAGE", "portico-worker")
    docker_network = config.get("dockerNetwork") or os.environ.get("DOCKER_NETWORK", "proxynet")
    ovpn_volume_name = config.get("dockerOvpnVolume") or os.environ.get("DOCKER_OVPN_VOLUME", "ovpn_data")

    port_base = max(1, min(65535, _cfg_int(config.get("portBase"), 50000)))
    internal_port_base = max(1, min(65535, _cfg_int(config.get("internalPortBase"), 51000)))
    # Host-side proxy port for location 0 when Docker publishes e.g. 51000->50000 (UI / curl on host).
    _ppb_env = (os.environ.get("PUBLISHED_PROXY_PORT_BASE") or "").strip()
    published_proxy_port_base: Optional[int] = None
    if _ppb_env.isdigit():
        published_proxy_port_base = int(_ppb_env)
    else:
        _ppb_cfg = config.get("publishedPortBase")
        if isinstance(_ppb_cfg, int) and _ppb_cfg > 0:
            published_proxy_port_base = _ppb_cfg
    max_slots = max(1, _cfg_int(config.get("maxSlots"), 50))
    idle_timeout_minutes = max(1, _cfg_int(config.get("idleTimeoutMinutes"), 45))
    idle_timeout_seconds = idle_timeout_minutes * 60.0
    listen_host = config.get("proxyListenHost") or "0.0.0.0"
    if use_docker:
        listen_host = "0.0.0.0"  # must listen on all interfaces so Docker port publishing works

    num_ports = 0 if auth_routing else len(locations)
    auth_http_port = _auth_http_port(config) if auth_routing else 0
    auth_socks_port = _auth_socks_port(config) if auth_routing else 0
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

    _docker_align = compute_docker_publish_alignment(port_base, num_ports, published_proxy_port_base)
    if _docker_align.get("publish_mismatch"):
        _log(f"Warning: Docker publish / config mismatch — {_docker_align.get('publish_mismatch_hint', '')}")
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

    auth_sockets_by_scheme: Dict[socket.socket, str] = {}
    if auth_routing:
        for port, scheme in ((auth_http_port, "http"), (auth_socks_port, "socks5")):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((listen_host, port))
                s.listen(128)
                s.setblocking(False)
                auth_sockets_by_scheme[s] = scheme
                listening_sockets.append(s)
            except OSError as e:
                print(f"Failed to bind {listen_host}:{port}: {e}", file=sys.stderr)
                for ss in listening_sockets:
                    try:
                        ss.close()
                    except Exception:
                        pass
                return 1
    elif num_ports > 0:
        _log(f"Listening on {listen_host}:{port_base}-{port_base + num_ports - 1} ({num_ports} ports)")
    else:
        _log("No proxy listener ports (0 locations); control API only.")
    if auth_routing:
        _log(f"Auth proxy listeners: HTTP {listen_host}:{auth_http_port}, SOCKS5 {listen_host}:{auth_socks_port}")
    _log(f"Backend: {'Docker' if use_docker else 'local'}; max_slots={max_slots} idle_timeout={idle_timeout_minutes}min")

    slots: List[Dict[str, Any]] = []
    port_to_slot: Dict[int, Dict[str, Any]] = {}
    active_ports: set = set()
    port_ovpn_assignment: Dict[int, str] = {}
    port_egress_by_port: Dict[int, Dict[str, str]] = {}
    assignments_path = resolve_assignments_path(config_path)
    upstream_catalog_path = resolve_upstream_catalog_path(config_path)
    if DB_STORE is not None:
        try:
            upstream_profiles = DB_STORE.load_upstream_profiles()
        except Exception as e:
            _log(f"Upstream proxy profile load from Postgres failed: {e}")
            upstream_profiles = []
    else:
        try:
            upstream_profiles = load_catalog(upstream_catalog_path)
        except UpstreamProxyError as e:
            _log(f"Upstream proxy catalog load failed: {e}")
            upstream_profiles = []
    upstream_profiles_by_id = _catalog_index(upstream_profiles)
    redis_url = _redis_url_from_env_or_config(config)
    redis_key = _redis_state_key()
    if redis_url:
        _log(f"Assignment store: Redis key={redis_key!r}")
    (
        _loaded_assign,
        _loaded_active_ports,
        _loaded_launcher_ids,
        _loaded_proxy_types,
        _loaded_rot_intervals,
        _loaded_rot_countries,
        _loaded_rot_last_run,
        _loaded_egress,
        _loaded_upstream_refresh_intervals,
        _loaded_upstream_refresh_last_run,
    ) = load_gateway_assignments_state(
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
    port_egress_by_port.update(_loaded_egress)
    launcher_ids_by_port: Dict[int, str] = dict(_loaded_launcher_ids)
    proxy_types_by_port: Dict[int, str] = dict(_loaded_proxy_types)
    rotation_intervals_by_port: Dict[int, int] = dict(_loaded_rot_intervals)
    rotation_countries_by_port: Dict[int, str] = dict(_loaded_rot_countries)
    rotation_last_run_by_port: Dict[int, float] = dict(_loaded_rot_last_run)
    upstream_refresh_intervals_by_port: Dict[int, int] = dict(_loaded_upstream_refresh_intervals)
    upstream_refresh_last_run_by_port: Dict[int, float] = dict(_loaded_upstream_refresh_last_run)
    _log(
        f"Assignments ({assignments_path}): loaded {len(_loaded_assign)} OVPN pick(s), "
        f"{len(_loaded_active_ports)} persisted active port(s), "
        f"{len(launcher_ids_by_port)} launcher ID(s), "
        f"{len(proxy_types_by_port)} SOCKS5 port override(s), "
        f"{len(rotation_intervals_by_port)} rotation rule(s), "
        f"{len(upstream_profiles_by_id)} upstream proxy profile(s), "
        f"{len(upstream_refresh_intervals_by_port)} upstream refresh rule(s)"
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
        "port_egress_by_port": port_egress_by_port,
        "launcher_ids_by_port": launcher_ids_by_port,
        "proxy_types_by_port": proxy_types_by_port,
        "rotation_intervals_by_port": rotation_intervals_by_port,
        "rotation_countries_by_port": rotation_countries_by_port,
        "rotation_last_run_by_port": rotation_last_run_by_port,
        "upstream_refresh_intervals_by_port": upstream_refresh_intervals_by_port,
        "upstream_refresh_last_run_by_port": upstream_refresh_last_run_by_port,
        "upstream_catalog_path": upstream_catalog_path,
        "upstream_profiles": upstream_profiles,
        "upstream_profiles_by_id": upstream_profiles_by_id,
        "activation_state_by_port": activation_state_by_port,
        "activation_error_by_port": activation_error_by_port,
        "activation_cancelled_ports": activation_cancelled_ports,
        "lock": lock,
        "config_path": config_path,
        "port_base": port_base,
        "max_slots": max_slots,
        "idle_timeout_minutes": idle_timeout_minutes,
        "use_docker": use_docker,
        "auth_routing": auth_routing,
        "local_auth_routing": local_auth_routing,
        "auth_routes": auth_routes,
        "auth_global_password": _auth_global_password(config),
        "auth_http_port": auth_http_port,
        "auth_socks_port": auth_socks_port,
        "auth_runtime_config": dict(config),
        "auth_route_state": {},
        "auth_route_error": {},
        "internal_port_base": internal_port_base,
        "docker_image": docker_image,
        "docker_network": docker_network,
        "ovpn_volume_name": ovpn_volume_name,
        "locations": locations,
        "listen_host": listen_host,
        "control_port": max(0, _cfg_int(config.get("controlPort"), CONTROL_PORT_DEFAULT)),
        "num_ports": num_ports,
        "proxy_username": (config.get("proxyUsername") or "").strip(),
        "proxy_password": config.get("proxyPassword") or "",
        "published_port_base": published_proxy_port_base,
        "assignments_path": assignments_path,
        "redis_url": redis_url,
        "redis_state_key": redis_key,
        "db_store": DB_STORE,
        **_docker_align,
    }

    idle_thread = threading.Thread(
        target=idle_eviction_loop,
        args=(gateway_state, idle_timeout_seconds, use_docker, port_base),
        daemon=True,
    )
    idle_thread.start()

    rotation_thread = threading.Thread(
        target=rotation_loop,
        args=(gateway_state,),
        daemon=True,
    )
    rotation_thread.start()

    upstream_refresh_thread = threading.Thread(
        target=upstream_refresh_loop,
        args=(gateway_state,),
        daemon=True,
    )
    upstream_refresh_thread.start()

    if auto_activate_on_startup and _loaded_active_ports and not auth_routing:
        _enforce_default_proxy_auth(config)
        _log(
            f"autoActivateOnStartup: bringing up {len(_loaded_active_ports)} persisted listener port(s)"
        )
        for port in _loaded_active_ports:
            loc_idx = port - port_base
            if loc_idx < 0 or loc_idx >= len(locations):
                continue
            egress = dict(port_egress_by_port.get(port) or {})
            if not egress:
                _log(f"Auto-activate skip port {port}: no egress assigned")
                continue
            err = validate_port_egress(
                config,
                config_path,
                loc_idx,
                use_docker,
                egress,
                gateway_state["upstream_profiles_by_id"],
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

    control_port = max(0, _cfg_int(gateway_state.get("control_port"), 0))
    if control_port > 0:
        gui_dir = script_dir() / "gui"
        control_thread = threading.Thread(
            target=_run_control_server,
            args=(control_port, gui_dir, gateway_state),
            daemon=True,
        )
        control_thread.start()
        _log(f"Control GUI: http://0.0.0.0:{control_port}")

    server_sockets = list(auth_sockets_by_scheme.keys()) if auth_routing else list(sockets_by_port.values())
    global shutdown_flag

    def _on_shutdown_signal(signum: int, frame: Any) -> None:
        global shutdown_flag
        shutdown_flag = True
        _log(f"Signal {signum} received, shutting down...")

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_shutdown_signal)
    signal.signal(signal.SIGINT, _on_shutdown_signal)

    def _accept_and_dispatch(sock: socket.socket) -> None:
        try:
            client_sock, _ = sock.accept()
        except OSError:
            return
        if auth_routing and sock in auth_sockets_by_scheme:
            scheme = auth_sockets_by_scheme[sock]
            target = handle_auth_socks_connection if scheme == "socks5" else handle_auth_http_connection
            t = threading.Thread(
                target=target,
                args=(client_sock, gateway_state),
                daemon=True,
            )
            t.start()
            return
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
                port_egress_by_port,
                gateway_state["upstream_profiles_by_id"],
                activation_state_by_port,
                lock,
                use_docker,
                docker_image,
                docker_network,
                ovpn_volume_name,
                gateway_state["proxy_types_by_port"],
            ),
            daemon=True,
        )
        t.start()

    try:
        # select() fails on Linux when any listener fd >= FD_SETSIZE (~1024); use poll-based API instead.
        if not server_sockets:
            while not shutdown_flag:
                time.sleep(1.0)
        else:
            sel = selectors.DefaultSelector()
            try:
                for s in server_sockets:
                    sel.register(s, selectors.EVENT_READ)
                while not shutdown_flag:
                    events = sel.select(timeout=1.0)
                    if shutdown_flag:
                        break
                    for key, _ in events:
                        sock = key.fileobj
                        if isinstance(sock, socket.socket):
                            _accept_and_dispatch(sock)
            finally:
                for s in server_sockets:
                    try:
                        sel.unregister(s)
                    except Exception:
                        pass
                try:
                    sel.close()
                except Exception:
                    pass
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
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(130)
    except BaseException:
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1)
