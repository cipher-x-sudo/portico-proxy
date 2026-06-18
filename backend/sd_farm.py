from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
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
    return re.sub(r"\s+", " ", stem).casefold()


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


def _json_post(base_url: str, action: str, payload: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
    base = (base_url or "").rstrip("/") + "/"
    url = base + action.lstrip("/")
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


def fetch_ixbrowser_profiles(base_url: str, page_limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(500, int(page_limit or 100)))
    page = 1
    profiles: List[Dict[str, Any]] = []
    total: Optional[int] = None
    while True:
        raw = _json_post(base_url, "profile-list", {"page": page, "limit": limit})
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


def update_ixbrowser_profile_proxy(
    base_url: str,
    profile_id_value: str,
    proxy_host: str,
    proxy_port: int,
    proxy_user: str,
    proxy_password: str,
) -> Dict[str, Any]:
    if not str(profile_id_value or "").strip():
        raise IXBrowserError("Missing ixBrowser profile id")
    payload = {
        "profile_id": profile_id_value,
        "proxy_info": {
            "proxy_mode": 2,
            "proxy_type": "http",
            "proxy_ip": proxy_host,
            "proxy_port": int(proxy_port),
            "proxy_user": proxy_user,
            "proxy_password": proxy_password,
            "proxy_check_line": "global_line",
        },
    }
    raw = _json_post(base_url, "profile-update-proxy-for-custom-proxy", payload)
    return {"ok": True, "response": _response_data(raw)}
