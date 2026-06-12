from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    "ovpnRoot": "../ovpn",
    "portBase": 50000,
    "proxyUsername": "",
    "proxyPassword": "",
    "autoDetectClientProxyHost": True,
    "clientProxyHost": "",
    "proxyListenHost": "127.0.0.1",
    "internalPortBase": 51000,
    "maxSlots": 50,
    "idleTimeoutMinutes": 45,
    "autoActivateOnStartup": True,
    "randomizeCountry": "US",
    "controlPort": 49999,
    "saveRunFile": False,
    "username": "",
    "password": "",
    "locationSpec": {
        "count": 128,
        "defaultOvpn": "NC/NCVPN-US-Chicago-UDP.ovpn",
        "labelPrefix": "proxy",
        "randomAccessFirstN": 3,
    },
}


def database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def enabled() -> bool:
    return bool(database_url())


class PorticoStore:
    def __init__(self, url: str):
        self.url = url

    def _connect(self):
        import psycopg2

        return psycopg2.connect(self.url)

    def _json(self, value: Any):
        from psycopg2.extras import Json

        return Json(value)

    def initialize(
        self,
        config_path: Optional[Path] = None,
        assignments_path: Optional[Path] = None,
        upstream_catalog_path: Optional[Path] = None,
        retries: int = 30,
    ) -> None:
        last_error: Optional[Exception] = None
        for _ in range(max(1, retries)):
            try:
                with self._connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS app_config (
                                key text PRIMARY KEY,
                                value jsonb NOT NULL,
                                updated_at timestamptz NOT NULL DEFAULT now()
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS port_state (
                                port integer PRIMARY KEY,
                                egress jsonb NOT NULL,
                                active boolean NOT NULL DEFAULT false,
                                launcher_id text,
                                proxy_type text,
                                rotation_interval_minutes integer,
                                rotation_country text,
                                rotation_last_run double precision,
                                upstream_refresh_interval_minutes integer,
                                upstream_refresh_last_run double precision,
                                updated_at timestamptz NOT NULL DEFAULT now()
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS upstream_proxy_profiles (
                                id text PRIMARY KEY,
                                label text NOT NULL,
                                scheme text NOT NULL,
                                host text NOT NULL,
                                port integer NOT NULL,
                                username text NOT NULL DEFAULT '',
                                password text NOT NULL DEFAULT '',
                                updated_at timestamptz NOT NULL DEFAULT now()
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS auth_routes (
                                username text PRIMARY KEY,
                                route_order integer NOT NULL,
                                label text NOT NULL,
                                external_id text NOT NULL DEFAULT '',
                                proxy_type text NOT NULL DEFAULT 'http',
                                rotation_interval_minutes integer,
                                rotation_country text,
                                rotation_last_run double precision,
                                enabled boolean NOT NULL,
                                egress jsonb NOT NULL,
                                updated_at timestamptz NOT NULL DEFAULT now()
                            )
                            """
                        )
                        cur.execute("ALTER TABLE auth_routes ADD COLUMN IF NOT EXISTS external_id text NOT NULL DEFAULT ''")
                        cur.execute("ALTER TABLE auth_routes ADD COLUMN IF NOT EXISTS proxy_type text NOT NULL DEFAULT 'http'")
                        cur.execute("ALTER TABLE auth_routes ADD COLUMN IF NOT EXISTS rotation_interval_minutes integer")
                        cur.execute("ALTER TABLE auth_routes ADD COLUMN IF NOT EXISTS rotation_country text")
                        cur.execute("ALTER TABLE auth_routes ADD COLUMN IF NOT EXISTS rotation_last_run double precision")
                        cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS provider_credentials (
                                provider text PRIMARY KEY,
                                username text NOT NULL,
                                password text NOT NULL,
                                updated_at timestamptz NOT NULL DEFAULT now()
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS ovpn_settings (
                                key text PRIMARY KEY,
                                value jsonb NOT NULL,
                                updated_at timestamptz NOT NULL DEFAULT now()
                            )
                            """
                        )
                    conn.commit()
                self.seed_if_empty(config_path, assignments_path, upstream_catalog_path)
                return
            except Exception as e:
                last_error = e
                time.sleep(1)
        raise RuntimeError(f"Could not initialize Postgres storage: {last_error}")

    def seed_if_empty(
        self,
        config_path: Optional[Path] = None,
        assignments_path: Optional[Path] = None,
        upstream_catalog_path: Optional[Path] = None,
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key = 'main'")
                if cur.fetchone() is not None:
                    return
                config = dict(DEFAULT_CONFIG)
                if config_path and config_path.is_file():
                    try:
                        config = json.loads(config_path.read_text(encoding="utf-8"))
                    except Exception:
                        config = dict(DEFAULT_CONFIG)
                cur.execute(
                    """
                    INSERT INTO app_config (key, value, updated_at)
                    VALUES ('main', %s, now())
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (self._json(config),),
                )
                if assignments_path and assignments_path.is_file():
                    try:
                        self._insert_assignment_payload(cur, json.loads(assignments_path.read_text(encoding="utf-8")))
                    except Exception:
                        pass
                if upstream_catalog_path and upstream_catalog_path.is_file():
                    try:
                        raw = json.loads(upstream_catalog_path.read_text(encoding="utf-8"))
                        profiles = raw.get("proxies") if isinstance(raw, dict) else []
                        if isinstance(profiles, list):
                            self._replace_upstream_profiles(cur, profiles)
                    except Exception:
                        pass
            conn.commit()

    def load_config(self) -> Dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key = 'main'")
                row = cur.fetchone()
                if not row:
                    return dict(DEFAULT_CONFIG)
                value = row[0]
                if isinstance(value, str):
                    return json.loads(value)
                return dict(value)

    def save_config(self, config: Dict[str, Any]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_config (key, value, updated_at)
                    VALUES ('main', %s, now())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                    """,
                    (self._json(config),),
                )
            conn.commit()

    def load_assignment_payload(self, port_base: int, num_ports: int) -> Dict[str, Any]:
        port_max = port_base + max(0, num_ports) - 1
        payload: Dict[str, Any] = {"version": 2, "assignments": {}, "activePorts": []}
        if num_ports <= 0:
            return payload
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT port, egress, active, launcher_id, proxy_type, rotation_interval_minutes,
                           rotation_country, rotation_last_run, upstream_refresh_interval_minutes,
                           upstream_refresh_last_run
                    FROM port_state
                    WHERE port BETWEEN %s AND %s
                    ORDER BY port
                    """,
                    (port_base, port_max),
                )
                for row in cur.fetchall():
                    port = int(row[0])
                    egress = row[1] if isinstance(row[1], dict) else json.loads(row[1])
                    if row[2]:
                        payload["activePorts"].append(port)
                    if egress:
                        payload.setdefault("egress", {})[str(port)] = egress
                        if egress.get("type") == "ovpn" and egress.get("ovpn"):
                            payload["assignments"][str(port)] = egress["ovpn"]
                    if row[3]:
                        payload.setdefault("launcherIds", {})[str(port)] = row[3]
                    if row[4] == "socks5":
                        payload.setdefault("proxyTypes", {})[str(port)] = "socks5"
                    if row[5]:
                        payload.setdefault("rotationIntervals", {})[str(port)] = int(row[5])
                    if row[6]:
                        payload.setdefault("rotationCountries", {})[str(port)] = row[6]
                    if row[7]:
                        payload.setdefault("rotationLastRun", {})[str(port)] = float(row[7])
                    if row[8]:
                        payload.setdefault("upstreamRefreshIntervals", {})[str(port)] = int(row[8])
                    if row[9]:
                        payload.setdefault("upstreamRefreshLastRun", {})[str(port)] = float(row[9])
        return payload

    def save_assignment_payload(self, payload: Dict[str, Any], port_base: int, num_ports: int) -> None:
        if num_ports <= 0:
            return
        port_max = port_base + num_ports - 1
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM port_state WHERE port BETWEEN %s AND %s", (port_base, port_max))
                self._insert_assignment_payload(cur, payload, port_base, port_max)
            conn.commit()

    def _insert_assignment_payload(
        self,
        cur,
        payload: Dict[str, Any],
        port_base: int = 1,
        port_max: int = 65535,
    ) -> None:
        if not isinstance(payload, dict):
            return
        active = {int(p) for p in payload.get("activePorts", []) if _is_int_like(p)}
        assignments = payload.get("assignments") if isinstance(payload.get("assignments"), dict) else {}
        egress_by_port = payload.get("egress") if isinstance(payload.get("egress"), dict) else {}
        keys = set(assignments.keys()) | set(egress_by_port.keys())
        for optional_key in (
            "launcherIds",
            "proxyTypes",
            "rotationIntervals",
            "rotationCountries",
            "rotationLastRun",
            "upstreamRefreshIntervals",
            "upstreamRefreshLastRun",
        ):
            block = payload.get(optional_key)
            if isinstance(block, dict):
                keys.update(block.keys())
        keys.update(str(p) for p in active)
        for key in keys:
            if not _is_int_like(key):
                continue
            port = int(key)
            if port < port_base or port > port_max:
                continue
            egress = egress_by_port.get(str(port)) or {}
            if not egress and assignments.get(str(port)):
                egress = {"type": "ovpn", "ovpn": assignments[str(port)]}
            if not egress:
                egress = {"type": "none"}
            cur.execute(
                """
                INSERT INTO port_state (
                    port, egress, active, launcher_id, proxy_type,
                    rotation_interval_minutes, rotation_country, rotation_last_run,
                    upstream_refresh_interval_minutes, upstream_refresh_last_run, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (port) DO UPDATE SET
                    egress = EXCLUDED.egress,
                    active = EXCLUDED.active,
                    launcher_id = EXCLUDED.launcher_id,
                    proxy_type = EXCLUDED.proxy_type,
                    rotation_interval_minutes = EXCLUDED.rotation_interval_minutes,
                    rotation_country = EXCLUDED.rotation_country,
                    rotation_last_run = EXCLUDED.rotation_last_run,
                    upstream_refresh_interval_minutes = EXCLUDED.upstream_refresh_interval_minutes,
                    upstream_refresh_last_run = EXCLUDED.upstream_refresh_last_run,
                    updated_at = now()
                """,
                (
                    port,
                    self._json(egress),
                    port in active,
                    _dict_get(payload, "launcherIds", str(port)),
                    _dict_get(payload, "proxyTypes", str(port)),
                    _int_or_none(_dict_get(payload, "rotationIntervals", str(port))),
                    _dict_get(payload, "rotationCountries", str(port)),
                    _float_or_none(_dict_get(payload, "rotationLastRun", str(port))),
                    _int_or_none(_dict_get(payload, "upstreamRefreshIntervals", str(port))),
                    _float_or_none(_dict_get(payload, "upstreamRefreshLastRun", str(port))),
                ),
            )

    def load_upstream_profiles(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, label, scheme, host, port, username, password
                    FROM upstream_proxy_profiles
                    ORDER BY updated_at, id
                    """
                )
                return [
                    {
                        "id": row[0],
                        "label": row[1],
                        "scheme": row[2],
                        "host": row[3],
                        "port": int(row[4]),
                        "username": row[5] or "",
                        "password": row[6] or "",
                    }
                    for row in cur.fetchall()
                ]

    def save_upstream_profiles(self, profiles: Iterable[Dict[str, Any]]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._replace_upstream_profiles(cur, profiles)
            conn.commit()

    def _replace_upstream_profiles(self, cur, profiles: Iterable[Dict[str, Any]]) -> None:
        cur.execute("DELETE FROM upstream_proxy_profiles")
        for profile in profiles:
            cur.execute(
                """
                INSERT INTO upstream_proxy_profiles
                    (id, label, scheme, host, port, username, password, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                """,
                (
                    profile.get("id") or "",
                    profile.get("label") or "",
                    profile.get("scheme") or "http",
                    profile.get("host") or "",
                    int(profile.get("port") or 0),
                    profile.get("username") or "",
                    profile.get("password") or "",
                ),
            )

    def load_auth_routes(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT username, route_order, label, external_id, proxy_type,
                           rotation_interval_minutes, rotation_country, rotation_last_run,
                           enabled, egress
                    FROM auth_routes
                    ORDER BY route_order, username
                    """
                )
                routes = []
                for row in cur.fetchall():
                    egress = row[9] if isinstance(row[9], dict) else json.loads(row[9])
                    routes.append(
                        {
                            "index": int(row[1]),
                            "username": row[0],
                            "label": row[2],
                            "externalId": row[3] or "",
                            "proxyType": "socks5" if row[4] == "socks5" else "http",
                            "rotationIntervalMinutes": int(row[5] or 0),
                            "rotationCountry": row[6] or "",
                            "rotationLastRun": float(row[7] or 0.0),
                            "enabled": bool(row[8]),
                            "egress": egress,
                        }
                    )
                return routes

    def save_auth_routes(self, routes: Iterable[Dict[str, Any]]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM auth_routes")
                for i, route in enumerate(routes):
                    cur.execute(
                        """
                        INSERT INTO auth_routes
                            (username, route_order, label, external_id, proxy_type,
                             rotation_interval_minutes, rotation_country, rotation_last_run,
                             enabled, egress, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                        """,
                        (
                            route.get("username") or "",
                            int(route.get("index", i)),
                            route.get("label") or route.get("username") or "",
                            route.get("externalId") or route.get("external_id") or "",
                            "socks5" if route.get("proxyType") == "socks5" else "http",
                            _int_or_none(route.get("rotationIntervalMinutes")),
                            route.get("rotationCountry") or "",
                            _float_or_none(route.get("rotationLastRun")),
                            bool(route.get("enabled", True)),
                            self._json(dict(route.get("egress") or {"type": "none"})),
                        ),
                    )
            conn.commit()

    def load_provider_credentials(self) -> Dict[str, Dict[str, str]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT provider, username, password
                    FROM provider_credentials
                    ORDER BY provider
                    """
                )
                out: Dict[str, Dict[str, str]] = {}
                for provider, username, password in cur.fetchall():
                    key = str(provider or "")
                    row = {"username": username or "", "password": password or ""}
                    out[key] = row
                    out[key.casefold()] = row
                return out

    def save_provider_credentials(self, credentials: Iterable[Dict[str, str]]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM provider_credentials")
                for row in credentials:
                    provider = (row.get("provider") or "").strip()
                    if not provider:
                        continue
                    cur.execute(
                        """
                        INSERT INTO provider_credentials
                            (provider, username, password, updated_at)
                        VALUES (%s, %s, %s, now())
                        ON CONFLICT (provider) DO UPDATE SET
                            username = EXCLUDED.username,
                            password = EXCLUDED.password,
                            updated_at = now()
                        """,
                        (
                            provider,
                            row.get("username") or "",
                            row.get("password") or "",
                        ),
                    )
            conn.commit()

    def upsert_provider_credential(self, provider: str, username: str, password: str) -> None:
        provider = (provider or "").strip()
        if not provider:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO provider_credentials
                        (provider, username, password, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (provider) DO UPDATE SET
                        username = EXCLUDED.username,
                        password = EXCLUDED.password,
                        updated_at = now()
                    """,
                    (provider, username or "", password or ""),
                )
            conn.commit()


def _is_int_like(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _dict_get(payload: Dict[str, Any], block_name: str, key: str) -> Any:
    block = payload.get(block_name)
    return block.get(key) if isinstance(block, dict) else None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None
