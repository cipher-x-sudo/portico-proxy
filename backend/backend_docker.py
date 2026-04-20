"""
Docker backend for the gateway: start/stop worker containers instead of local processes.
"""

import os
import re
import socket
import sys
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from provider_auth import load_provider_auth

WORKER_PROXY_PORT = 8080
# Compose does not manage dynamically started workers; label them for teardown (compose down scripts / gateway exit).
WORKER_CONTAINER_LABEL = "portico.proxy.worker"
_PROXY_NAME_RE = re.compile(r"^proxy-\d+$")
DEFAULT_PROXY_USERNAME = "huzaifa"
DEFAULT_PROXY_PASSWORD = "huzaifa"


def _log(msg: str) -> None:
    print(f"[Docker backend] {msg}", flush=True, file=sys.stderr)


def _resolve_network(client: Any, preferred: str) -> str:
    """
    Resolve the actual Docker network name. Compose creates networks like '<project>_proxynet' (e.g. proxy-test_proxynet).
    Use the network the gateway container is attached to so workers can be reached.
    """
    try:
        hostname = socket.gethostname()
        _log(f"Resolving network (preferred={preferred}); gateway hostname={hostname}")
        me = client.containers.get(hostname)
        networks = list(me.attrs.get("NetworkSettings", {}).get("Networks", {}).keys())
        if networks:
            # Prefer one whose name contains the preferred name (e.g. proxynet)
            for n in networks:
                if preferred in n or n == preferred:
                    _log(f"Using gateway network: {n}")
                    return n
            chosen = networks[0]
            _log(f"Using gateway network (first): {chosen}")
            return chosen
    except Exception as e:
        _log(f"Could not resolve gateway network from container: {e}")
    # Fallback: list all networks and pick one containing preferred
    try:
        for net in client.networks.list():
            name = net.name
            if preferred in name or name == preferred:
                _log(f"Using discovered network: {name}")
                return name
    except Exception as e:
        _log(f"Could not discover network: {e}")
    _log(f"Falling back to preferred name as-is: {preferred}")
    return preferred


def start_docker_backend(
    location_index: int,
    external_port: int,
    config: dict,
    docker_image: str,
    docker_network: str,
    ovpn_volume_name: str,
    proxy_listen_scheme: str = "http",
) -> Tuple[str, int]:
    """
    Start a worker container for the given location. Returns (backend_host, backend_port).
    backend_host is the container name (e.g. proxy-50004) so the gateway can connect on the shared network.
    """
    try:
        import docker
    except ImportError:
        raise RuntimeError("Docker backend requires the 'docker' package. pip install docker")

    _log(f"Starting worker for location_index={location_index} external_port={external_port}")

    locations = config.get("locations") or []
    if location_index < 0 or location_index >= len(locations):
        raise IndexError(f"location_index {location_index} out of range")
    loc = locations[location_index]
    ovpn_file = loc.get("ovpn", "")
    auth_user = ""
    auth_pass = ""
    provider_auth = load_provider_auth(ovpn_file, Path("/ovpn"))
    auth_user = provider_auth.username
    auth_pass = provider_auth.password

    proxy_user = (config.get("proxyUsername") or "").strip()
    proxy_pass = (config.get("proxyPassword") or "")
    if not proxy_user or not proxy_pass:
        proxy_user = DEFAULT_PROXY_USERNAME
        proxy_pass = DEFAULT_PROXY_PASSWORD

    container_name = f"proxy-{external_port}"
    _log(f"Connecting to Docker daemon")
    client = docker.from_env()

    # Resolve actual network name (Compose uses project_prefix + network name)
    resolved_network = _resolve_network(client, docker_network)
    _log(f"Resolved network: {resolved_network}")

    # Remove if a previous container with this name exists (e.g. from a crash)
    try:
        old = client.containers.get(container_name)
        _log(f"Removing existing container: {container_name}")
        old.remove(force=True)
    except Exception:
        pass

    scheme = (proxy_listen_scheme or "http").strip().lower()
    if scheme not in ("http", "socks5"):
        scheme = "http"
    env = [
        f"OVPN_FILE={ovpn_file}",
        f"AUTH_USER={auth_user}",
        f"AUTH_PASS={auth_pass}",
        f"PROXY_USER={proxy_user}",
        f"PROXY_PASS={proxy_pass}",
        f"PROXY_LISTEN_SCHEME={scheme}",
    ]
    _log(f"Creating container name={container_name} image={docker_image} network={resolved_network} volume={ovpn_volume_name}:/ovpn:ro ovpn_file={ovpn_file}")
    try:
        # OpenVPN needs /dev/net/tun; pass host TUN device and volume via run() kwargs
        container = client.containers.run(
            docker_image,
            name=container_name,
            network=resolved_network,
            environment={e.split("=", 1)[0]: e.split("=", 1)[1] for e in env},
            volumes={ovpn_volume_name: {"bind": "/ovpn", "mode": "ro"}},
            cap_add=["NET_ADMIN"],
            devices=["/dev/net/tun:/dev/net/tun"],
            dns=["8.8.8.8"],
            detach=True,
            remove=False,
            labels={WORKER_CONTAINER_LABEL: "true"},
        )
        _log(f"Worker container started: {container_name} (id={container.short_id}) -> {container_name}:{WORKER_PROXY_PORT}")
    except Exception as e:
        _log(f"containers.run failed: {e}")
        raise
    return (container_name, WORKER_PROXY_PORT)


def teardown_docker_backend(container_name: str) -> None:
    """Stop and remove a worker container by name."""
    try:
        import docker
    except ImportError:
        return
    _log(f"Tearing down worker container: {container_name}")
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
        container.stop(timeout=5)
        container.remove(force=True)
        _log(f"Stopped and removed container: {container_name}")
    except Exception as e:
        err_str = str(e).strip()
        if "404" in err_str or "No such container" in err_str or "not found" in err_str.lower():
            _log(f"Teardown {container_name}: container already removed")
        else:
            _log(f"Teardown {container_name}: {e}")


def remove_all_dynamic_worker_containers() -> List[str]:
    """
    Stop and remove every gateway-spawned worker (labeled or named proxy-<port>).
    Used on gateway shutdown so proxynet is not left busy after docker compose down.
    """
    removed: List[str] = []
    try:
        import docker
    except ImportError:
        return removed
    try:
        client = docker.from_env()
    except Exception:
        return removed
    try:
        for c in client.containers.list(all=True):
            name = c.name or ""
            labeled = (c.labels or {}).get(WORKER_CONTAINER_LABEL, "").lower() == "true"
            if not labeled and not _PROXY_NAME_RE.match(name):
                continue
            nm = name or c.short_id
            try:
                c.stop(timeout=5)
                c.remove(force=True)
                removed.append(nm)
            except Exception as e:
                _log(f"Could not remove worker {nm}: {e}")
    except Exception as e:
        _log(f"Dynamic worker cleanup: {e}")
    if removed:
        _log(f"Removed {len(removed)} dynamic worker container(s)")
    return removed


def get_worker_logs(container_name: str) -> Optional[str]:
    """Return stdout+stderr of a worker container if it exists, else None."""
    try:
        import docker
    except ImportError:
        return None
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
        return container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
    except Exception:
        return None
