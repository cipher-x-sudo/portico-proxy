#!/usr/bin/env python3
"""
Reusable logic to start one OpenVPN + pproxy for a single location.
Used by the dynamic gateway (gateway.py) for on-demand proxy startup.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple
from provider_auth import load_provider_auth

MARKER = "Initialization Sequence Completed"
DEFAULT_PROXY_USERNAME = "huzaifa"
DEFAULT_PROXY_PASSWORD = "huzaifa"
PRIVATE_IP_RE = re.compile(
    r"^(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3})$"
)


def _script_dir(config_path: Path) -> Path:
    """Script dir is the parent of the config file (typically the backend folder)."""
    return config_path.resolve().parent


def resolve_ovpn_path(path: str, base_path: Optional[Path], script_dir_path: Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    root = base_path if base_path is not None else script_dir_path
    full = root / path
    return full if full.exists() else p


def wait_openvpn_ready(log_path: Path, timeout_seconds: int = 120) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if log_path.exists():
            try:
                content = log_path.read_text(errors="replace")
                if MARKER in content:
                    return True
            except OSError:
                pass
        time.sleep(2)
    return False


def get_vpn_ip_from_log(log_path: Path) -> Optional[str]:
    if not log_path.exists():
        return None
    try:
        content = log_path.read_text(errors="replace")
    except OSError:
        return None
    for match in re.finditer(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", content):
        ip = match.group(0)
        if PRIVATE_IP_RE.match(ip):
            return ip
    return None


def start_one_location(
    config: dict,
    location_index: int,
    internal_port: int,
    config_path: Path,
    listen_scheme: str = "http",
) -> Tuple[subprocess.Popen, subprocess.Popen, str, str]:
    """
    Start OpenVPN and pproxy for one location. Uses ForceBindIP so traffic exits via that VPN.
    Returns (openvpn_process, proxy_process, log_path, auth_path).
    Caller must terminate processes and delete log_path/auth_path on teardown.
    """
    script_dir_path = _script_dir(config_path)
    locations = config.get("locations") or []
    if location_index < 0 or location_index >= len(locations):
        raise IndexError(f"location_index {location_index} out of range (0..{len(locations)-1})")
    loc = locations[location_index]

    openvpn_exe = config.get("openvpnPath") or "openvpn"
    if openvpn_exe != "openvpn" and not Path(openvpn_exe).is_absolute():
        openvpn_exe = str(script_dir_path / openvpn_exe)
    force_bind_ip = (config.get("forceBindIPPath") or "").strip()
    if force_bind_ip and not Path(force_bind_ip).is_absolute():
        force_bind_ip = str(script_dir_path / force_bind_ip)
    ovpn_root = None
    if config.get("ovpnRoot"):
        ovpn_root = script_dir_path / config["ovpnRoot"]
    python_path = config.get("pythonPath") or "python"
    # Internal proxy for gateway: always bind to localhost only
    proxy_listen_host = "127.0.0.1"
    proxy_user = (config.get("proxyUsername") or "").strip()
    proxy_pass = config.get("proxyPassword") or ""
    # Enforce required proxy auth. If config omits either field, fall back to
    # temporary defaults selected by the user.
    if not proxy_user or not proxy_pass:
        proxy_user = DEFAULT_PROXY_USERNAME
        proxy_pass = DEFAULT_PROXY_PASSWORD

    ovpn_full = resolve_ovpn_path(loc["ovpn"], ovpn_root, script_dir_path)
    if not ovpn_full.exists():
        raise FileNotFoundError(f"OVPN not found: {loc['ovpn']} (resolved: {ovpn_full})")

    fd, log_file = tempfile.mkstemp(prefix=f"openvpn-gateway-{location_index}-", suffix=".log")
    os.close(fd)
    log_path = Path(log_file)

    openvpn_cmd = [
        openvpn_exe,
        "--config", str(ovpn_full),
        "--log", str(log_path.resolve()),
    ]
    auth_file = None
    auth_user = ""
    auth_pass = ""
    if ovpn_root is None:
        raise RuntimeError("ovpnRoot is required for provider auth resolution.")
    provider_auth = load_provider_auth(loc.get("ovpn") or "", ovpn_root)
    auth_user = provider_auth.username
    auth_pass = provider_auth.password
    if auth_user:
        fd_auth, auth_file = tempfile.mkstemp(prefix="openvpn-auth-", suffix=".txt")
        try:
            with os.fdopen(fd_auth, "w", encoding="utf-8") as f:
                f.write(auth_user + "\n")
                f.write(auth_pass or "")
        except Exception:
            os.close(fd_auth)
            raise
        openvpn_cmd.extend(["--auth-user-pass", auth_file])

    try:
        openvpn_process = subprocess.Popen(openvpn_cmd)
    except FileNotFoundError as e:
        if auth_file:
            try:
                Path(auth_file).unlink(missing_ok=True)
            except OSError:
                pass
        raise RuntimeError(
            "OpenVPN not found. Set openvpnPath in config or add OpenVPN to PATH."
        ) from e

    if not wait_openvpn_ready(log_path):
        openvpn_process.terminate()
        try:
            openvpn_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            openvpn_process.kill()
        if auth_file:
            try:
                Path(auth_file).unlink(missing_ok=True)
            except OSError:
                pass
        raise RuntimeError(
            f"OpenVPN did not report ready within timeout for location {location_index}."
        )

    vpn_ip = get_vpn_ip_from_log(log_path)
    if not vpn_ip:
        openvpn_process.terminate()
        try:
            openvpn_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            openvpn_process.kill()
        if auth_file:
            try:
                Path(auth_file).unlink(missing_ok=True)
            except OSError:
                pass
        raise RuntimeError(f"Could not detect VPN IP from log for location {location_index}.")

    scheme = (listen_scheme or "http").strip().lower()
    if scheme not in ("http", "socks5"):
        scheme = "http"
    # Gateway internal proxies; listen on 127.0.0.1:internal_port (http or socks5, mutually exclusive)
    listen_uri = f"{scheme}://{proxy_listen_host}:{internal_port}#{proxy_user}:{proxy_pass}"
    pproxy_args = ["-m", "pproxy", "-l", listen_uri]

    if force_bind_ip and Path(force_bind_ip).exists():
        cmd = [force_bind_ip, vpn_ip, python_path] + pproxy_args
        try:
            proxy_process = subprocess.Popen(cmd)
        except FileNotFoundError:
            cmd = [force_bind_ip, vpn_ip, subprocess.sys.executable] + pproxy_args
            proxy_process = subprocess.Popen(cmd)
    else:
        try:
            proxy_process = subprocess.Popen([python_path] + pproxy_args)
        except FileNotFoundError:
            proxy_process = subprocess.Popen([sys.executable] + pproxy_args)

    return (openvpn_process, proxy_process, log_file, auth_file or "")
