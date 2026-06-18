import json
import os
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import gateway  # noqa: E402
import sd_farm  # noqa: E402


def _create_accounts_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE accounts (
                Current_Status TEXT,
                Name TEXT,
                UID TEXT PRIMARY KEY,
                Status TEXT,
                Proxy TEXT,
                OpenVPN TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO accounts (UID, Name, OpenVPN, Proxy, Status, Current_Status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("61560173093090", "Ali Khan", "NCVPN-US-Phoenix-UDP", "", "Live", "Success"),
        )
        conn.commit()
    finally:
        conn.close()


class SDFarmTests(unittest.TestCase):
    def test_discovers_and_loads_accounts_db_read_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "DB" / "data" / "accounts.sqlite"
            _create_accounts_db(db)

            found, candidates = sd_farm.discover_accounts_db(root)
            rows = sd_farm.load_accounts(found)

            self.assertEqual(found, db)
            self.assertEqual(candidates, [db])
            self.assertEqual(rows[0]["UID"], "61560173093090")
            self.assertEqual(rows[0]["OpenVPN"], "NCVPN-US-Phoenix-UDP")

    def test_browse_directory_lists_child_folders_and_db_hint(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "DB" / "data" / "accounts.sqlite"
            _create_accounts_db(db)
            (root / "empty").mkdir()

            payload = sd_farm.browse_directory(root)

            self.assertEqual(Path(payload["path"]).resolve(), root.resolve())
            self.assertTrue(payload["hasAccountsDb"])
            self.assertEqual(Path(payload["accountsDbPath"]).resolve(), db.resolve())
            names = [entry["name"] for entry in payload["entries"]]
            self.assertIn("DB", names)
            self.assertIn("empty", names)
            db_entry = next(entry for entry in payload["entries"] if entry["name"] == "DB")
            self.assertTrue(db_entry["hasAccountsDb"])
            self.assertEqual(Path(db_entry["accountsDbPath"]).resolve(), db.resolve())

    def test_save_imported_accounts_db_writes_and_validates(self):
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "accounts.sqlite"
            db = Path(tmp) / "source" / "DB" / "data" / "accounts.sqlite"
            _create_accounts_db(db)
            data = db.read_bytes()
            count = sd_farm.save_imported_accounts_db(data, dest)
            self.assertEqual(count, 1)
            self.assertTrue(dest.is_file())
            rows = sd_farm.load_accounts(dest)
            self.assertEqual(rows[0]["UID"], "61560173093090")

    def test_matches_sd_farm_openvpn_to_portico_ovpn_file(self):
        matched, err = sd_farm.match_ovpn(
            "NCVPN-US-Phoenix-UDP",
            ["NC/NCVPN-US-Phoenix-UDP.ovpn", "NC/NCVPN-US-NewYork-UDP.ovpn"],
        )

        self.assertEqual(matched, "NC/NCVPN-US-Phoenix-UDP.ovpn")
        self.assertEqual(err, "")

    def test_matches_sd_farm_openvpn_to_compact_portico_filename(self):
        matched, err = sd_farm.match_ovpn(
            "NCVPN-US-NewYork-UDP",
            ["NC/NCVPN-US-NewYork-UDP.ovpn", "NC/NCVPN-US-NewOrleans-UDP.ovpn"],
        )

        self.assertEqual(matched, "NC/NCVPN-US-NewYork-UDP.ovpn")
        self.assertEqual(err, "")

        matched, err = sd_farm.match_ovpn(
            "NCVPN-US-NewOrleans-TCP",
            ["NC/NCVPN-US-NewYork-UDP.ovpn", "NC/NCVPN-US-NewOrleans-TCP.ovpn"],
        )

        self.assertEqual(matched, "NC/NCVPN-US-NewOrleans-TCP.ovpn")
        self.assertEqual(err, "")

    def test_account_rows_flag_duplicate_and_missing_browser_profiles(self):
        accounts = [
            {"UID": "111", "Name": "One", "OpenVPN": "NCVPN-US-Phoenix-UDP"},
            {"UID": "222", "Name": "Two", "OpenVPN": "NCVPN-US-Phoenix-UDP"},
            {"UID": "333", "Name": "Three", "OpenVPN": "NCVPN-US-Phoenix-UDP"},
        ]
        profiles = [
            {"profile_id": 10, "name": "fb 111 primary"},
            {"profile_id": 11, "name": "fb 222 a"},
            {"profile_id": 12, "name": "fb 222 b"},
        ]

        rows = sd_farm.build_account_rows(accounts, ["NC/NCVPN-US-Phoenix-UDP.ovpn"], profiles)

        self.assertTrue(rows[0]["valid"])
        self.assertEqual(rows[1]["browserStatus"], "duplicate")
        self.assertEqual(rows[2]["browserStatus"], "missing")

    def test_ixbrowser_update_sends_custom_proxy_payload(self):
        calls = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"error": {"code": 0}, "data": True}).encode("utf-8")

        def fake_urlopen(req, timeout):
            calls["url"] = req.full_url
            calls["timeout"] = timeout
            calls["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            result = sd_farm.update_ixbrowser_profile_proxy(
                "http://127.0.0.1:53200/api/v2/",
                "99",
                "127.0.0.1",
                58680,
                "sd_99",
                "secret",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(calls["url"], "http://127.0.0.1:53200/api/v2/profile-update-proxy-for-custom-proxy")
        self.assertEqual(calls["payload"]["profile_id"], 99)
        self.assertEqual(calls["payload"]["proxy_info"]["proxy_mode"], 2)
        self.assertEqual(calls["payload"]["proxy_info"]["proxy_type"], "http")
        self.assertEqual(calls["payload"]["proxy_info"]["proxy_user"], "sd_99")

    def test_ixbrowser_update_supports_socks5_proxy_type(self):
        calls = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"error": {"code": 0}, "data": True}).encode("utf-8")

        def fake_urlopen(req, timeout):
            calls["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            sd_farm.update_ixbrowser_profile_proxy(
                "http://127.0.0.1:53200/api/v2/",
                "99",
                "127.0.0.1",
                58681,
                "sd_99",
                "secret",
                proxy_type="socks5",
            )

        self.assertEqual(calls["payload"]["proxy_info"]["proxy_type"], "socks5")

    def test_normalize_ixbrowser_proxy_type(self):
        self.assertEqual(sd_farm.normalize_ixbrowser_proxy_type("socks5"), "socks5")
        self.assertEqual(sd_farm.normalize_ixbrowser_proxy_type("HTTP"), "http")
        self.assertEqual(sd_farm.normalize_ixbrowser_proxy_type(""), "http")

    def test_is_docker_bridge_ip(self):
        self.assertTrue(sd_farm._is_docker_bridge_ip("172.17.0.1"))
        self.assertFalse(sd_farm._is_docker_bridge_ip("172.22.192.1"))
        self.assertFalse(sd_farm._is_docker_bridge_ip("172.19.128.1"))
        self.assertFalse(sd_farm._is_docker_bridge_ip("10.255.255.254"))

    def test_is_docker_desktop_internal_ip(self):
        self.assertTrue(sd_farm._is_docker_desktop_internal_ip("192.168.65.1"))
        self.assertTrue(sd_farm._is_docker_desktop_internal_ip("192.168.65.254"))
        self.assertFalse(sd_farm._is_docker_desktop_internal_ip("172.19.128.1"))

    def test_discover_wsl_windows_host_ip_rejects_docker_desktop_internal_probe(self):
        sd_farm._windows_host_ip_cache = None
        sd_farm._windows_host_ip_cache_attempted = False
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"192.168.65.1\n"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sd_farm, "_running_inside_docker", return_value=True),
            patch.object(sd_farm, "_docker_socket_available", return_value=True),
            patch.object(sd_farm, "_docker_client", return_value=mock_client),
        ):
            self.assertIsNone(sd_farm.discover_wsl_windows_host_ip(force_refresh=True))

    def test_discover_wsl_windows_host_ip_skips_loopback_resolv(self):
        sd_farm._windows_host_ip_cache = None
        sd_farm._windows_host_ip_cache_attempted = False
        with (
            patch.object(sd_farm, "_discover_windows_host_via_docker_host_network", return_value=None),
            patch.object(Path, "is_file", return_value=True),
            patch.object(
                Path,
                "read_text",
                return_value="nameserver 127.0.0.11\n",
            ),
        ):
            self.assertIsNone(sd_farm.discover_wsl_windows_host_ip(force_refresh=True))

    def test_discover_wsl_windows_host_ip_uses_non_loopback_nameserver(self):
        sd_farm._windows_host_ip_cache = None
        sd_farm._windows_host_ip_cache_attempted = False
        with (
            patch.object(sd_farm, "_discover_windows_host_via_docker_host_network", return_value=None),
            patch.object(Path, "is_file", return_value=True),
            patch.object(
                Path,
                "read_text",
                return_value="nameserver 10.255.255.254\n",
            ),
        ):
            self.assertEqual(sd_farm.discover_wsl_windows_host_ip(force_refresh=True), "10.255.255.254")

    def test_discover_wsl_windows_host_ip_uses_docker_host_network_probe(self):
        sd_farm._windows_host_ip_cache = None
        sd_farm._windows_host_ip_cache_attempted = False
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"172.19.128.1\n"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sd_farm, "_running_inside_docker", return_value=True),
            patch.object(sd_farm, "_docker_socket_available", return_value=True),
            patch.object(sd_farm, "_docker_client", return_value=mock_client),
        ):
            self.assertEqual(sd_farm.discover_wsl_windows_host_ip(force_refresh=True), "172.19.128.1")

    def test_discover_wsl_windows_host_ip_rejects_bridge_env_override(self):
        with patch.dict(os.environ, {"IXBROWSER_WINDOWS_HOST": "172.17.0.1"}, clear=False):
            self.assertIsNone(sd_farm.discover_wsl_windows_host_ip(force_refresh=True))

    def test_json_post_uses_url_literally_without_rewrite(self):
        payload = {"page": 1, "limit": 1}
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"error": {"code": 0}, "data": {"total": 0, "data": []}}).encode("utf-8")

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            return FakeResponse()

        with (
            patch.object(sd_farm, "_running_inside_docker", return_value=True),
            patch.object(sd_farm, "discover_wsl_windows_host_ip", return_value="172.19.128.1"),
            patch("urllib.request.urlopen", fake_urlopen),
        ):
            data = sd_farm._json_post("http://host.docker.internal:53200/api/v2/", "profile-list", payload)
        self.assertEqual(data["error"]["code"], 0)
        self.assertEqual(
            captured["url"],
            "http://host.docker.internal:53200/api/v2/profile-list",
        )

    def test_probe_ixbrowser_host_docker_internal_wins_on_docker_desktop(self):
        calls = []

        def fake_ping(base, timeout=4.0):
            calls.append(base)
            if base == "http://host.docker.internal:53200/api/v2/":
                return 18
            raise sd_farm.IXBrowserError("Connection refused")

        with (
            patch.object(sd_farm, "discover_wsl_windows_host_ip", return_value=None),
            patch.object(sd_farm, "_ping_ixbrowser_base", side_effect=fake_ping),
        ):
            status = sd_farm.probe_ixbrowser_bases(
                sd_farm.ixbrowser_api_candidates(
                    use_docker=True,
                    configured_base="http://host.docker.internal:53200/api/v2/",
                ),
                use_docker=True,
            )
        self.assertTrue(status["ok"])
        self.assertEqual(status["ixBrowserApiBase"], "http://host.docker.internal:53200/api/v2/")
        self.assertEqual(calls[0], "http://host.docker.internal:53200/api/v2/")

    def test_probe_ixbrowser_wsl_ip_wins_when_host_docker_internal_fails(self):
        calls = []

        def fake_ping(base, timeout=4.0):
            calls.append(base)
            if base == "http://172.19.128.1:53200/api/v2/":
                return 18
            raise sd_farm.IXBrowserError("Connection refused")

        with (
            patch.object(sd_farm, "discover_wsl_windows_host_ip", return_value="172.19.128.1"),
            patch.object(sd_farm, "_ping_ixbrowser_base", side_effect=fake_ping),
        ):
            status = sd_farm.probe_ixbrowser_bases(
                sd_farm.ixbrowser_api_candidates(
                    use_docker=True,
                    configured_base="http://host.docker.internal:53200/api/v2/",
                ),
                use_docker=True,
            )
        self.assertTrue(status["ok"])
        self.assertEqual(status["ixBrowserApiBase"], "http://172.19.128.1:53200/api/v2/")
        self.assertIn("http://host.docker.internal:53200/api/v2/", calls)
        self.assertIn("http://172.19.128.1:53200/api/v2/", calls)

    def test_discover_wsl_windows_host_ip_uses_env_override(self):
        sd_farm._windows_host_ip_cache = None
        sd_farm._windows_host_ip_cache_attempted = False
        with patch.dict(os.environ, {"IXBROWSER_WINDOWS_HOST": "172.19.128.1"}, clear=False):
            self.assertEqual(sd_farm.discover_wsl_windows_host_ip(force_refresh=True), "172.19.128.1")

    def test_resolve_route_username_uses_map(self):
        route_map = {"61560173093090": "rose_selma"}
        self.assertEqual(sd_farm.resolve_route_username("61560173093090", route_map), "rose_selma")
        self.assertEqual(sd_farm.resolve_route_username("999", route_map), "sd_999")

    def test_parse_route_import_text_csv(self):
        mapping, errors = sd_farm.parse_route_import_text(
            "uid,routeUsername,name\n61560173093090,rose_selma,Rose\n"
        )
        self.assertEqual(errors, [])
        self.assertEqual(mapping["61560173093090"], "rose_selma")

    def test_merge_route_map_rejects_duplicate_routes(self):
        merged, errors = sd_farm.merge_route_map(
            {},
            {"1": "same_route", "2": "same_route"},
            mode="merge",
        )
        self.assertIn("duplicate routeUsername", errors[0])
        self.assertEqual(merged.get("1"), "same_route")
        self.assertNotIn("2", merged)

    def test_export_route_map_csv(self):
        csv_text = sd_farm.export_route_map_csv(
            [{"uid": "61560173093090", "routeUsername": "rose_selma", "name": "Rose"}]
        )
        self.assertIn("61560173093090,rose_selma", csv_text)

    def test_ovpn_note_from_matched_path_strips_folder_and_extension(self):
        self.assertEqual(
            sd_farm.ovpn_note_from_matched_path("NC/NCVPN-US-NewYork-UDP.ovpn"),
            "NCVPN-US-NewYork-UDP",
        )
        self.assertEqual(
            sd_farm.ovpn_note_from_matched_path("NCVPN-US-Phoenix-UDP.ovpn"),
            "NCVPN-US-Phoenix-UDP",
        )
        self.assertEqual(sd_farm.ovpn_note_from_matched_path(""), "")

    def test_ixbrowser_update_note_sends_profile_update_payload(self):
        calls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"error": {"code": 0}, "data": True}).encode("utf-8")

        def fake_urlopen(req, timeout):
            calls.append(
                {
                    "url": req.full_url,
                    "payload": json.loads(req.data.decode("utf-8")),
                }
            )
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            result = sd_farm.update_ixbrowser_profile_note(
                "http://127.0.0.1:53200/api/v2/",
                "99",
                "NCVPN-US-NewYork-UDP",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "http://127.0.0.1:53200/api/v2/profile-update")
        self.assertEqual(calls[0]["payload"]["profile_id"], 99)
        self.assertEqual(calls[0]["payload"]["note"], "NCVPN-US-NewYork-UDP")

    def test_sync_ixbrowser_profile_updates_proxy_and_note(self):
        calls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"error": {"code": 0}, "data": True}).encode("utf-8")

        def fake_urlopen(req, timeout):
            calls.append(req.full_url)
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            result = sd_farm.sync_ixbrowser_profile(
                "http://127.0.0.1:53200/api/v2/",
                "99",
                "127.0.0.1",
                58680,
                "sd_99",
                "secret",
                "NC/NCVPN-US-NewYork-UDP.ovpn",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["note"], "NCVPN-US-NewYork-UDP")
        self.assertEqual(
            calls,
            [
                "http://127.0.0.1:53200/api/v2/profile-update-proxy-for-custom-proxy",
                "http://127.0.0.1:53200/api/v2/profile-update",
            ],
        )


class SDFarmGatewaySyncTests(unittest.TestCase):
    def test_sd_farm_root_prefers_config_over_env(self):
        with patch.dict(
            "os.environ",
            {"SD_FARM_ROOT": "/from-env"},
            clear=False,
        ):
            state = {
                "auth_runtime_config": {"sdFarmRoot": "C:/from-config"},
                "use_docker": False,
            }
            self.assertEqual(
                gateway._sd_farm_root_from_state(state),
                sd_farm.resolve_sd_farm_root("C:/from-config"),
            )

    def test_validate_and_persist_sd_farm_settings(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "DB" / "data" / "accounts.sqlite"
            _create_accounts_db(db)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            state = {
                "lock": threading.Lock(),
                "config_path": config_path,
                "db_store": None,
                "auth_runtime_config": {},
                "use_docker": False,
            }
            settings = {
                "sdFarmRoot": str(root),
                "ixBrowserApiBase": "http://127.0.0.1:53200/api/v2/",
                "ixBrowserProxyHost": "127.0.0.1",
                "ixBrowserProxyType": "socks5",
            }

            self.assertIsNone(gateway._validate_sd_farm_settings(state, settings))
            self.assertIsNone(gateway._persist_sd_farm_settings(config_path, state, settings))
            gateway._apply_sd_farm_settings(state, settings)

            with patch.object(
                gateway,
                "_probe_ixbrowser",
                return_value={"ok": False, "ixBrowserError": "", "ixBrowserProfileCount": 0, "triedUrls": []},
            ):
                payload = gateway._sd_farm_settings_payload(state)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["sdFarmRoot"], str(root))
            self.assertEqual(saved["ixBrowserProxyType"], "socks5")
            self.assertEqual(payload["dbPath"], str(db))

    def test_validate_import_label_does_not_require_server_path(self):
        state = {
            "auth_runtime_config": {"sdFarmSource": "import", "sdFarmRoot": r"D:\WORK\SD Farm"},
            "use_docker": True,
        }
        settings = {"ixBrowserApiBase": "http://127.0.0.1:53200/api/v2/"}
        self.assertIsNone(gateway._validate_sd_farm_settings(state, settings))

    def test_probe_ixbrowser_reports_connection_error(self):
        state = {"auth_runtime_config": {}, "use_docker": True}
        with (
            patch.object(sd_farm, "discover_wsl_windows_host_ip", return_value="10.255.255.254"),
            patch.object(sd_farm, "_ping_ixbrowser_base", side_effect=sd_farm.IXBrowserError("Connection refused")),
        ):
            status = gateway._probe_ixbrowser(state, api_base="http://127.0.0.1:53200/api/v2/")
        self.assertFalse(status["ok"])
        self.assertIn("Connection refused", status["ixBrowserError"])
        self.assertTrue(status.get("triedUrls"))
        self.assertEqual(status.get("recommendedBase"), "http://10.255.255.254:53200/api/v2/")

    def test_probe_ixbrowser_tries_fallback_url(self):
        calls = []

        def fake_ping(base, timeout=4.0):
            calls.append(base)
            if base == "http://10.255.255.254:53200/api/v2/":
                return 1
            raise sd_farm.IXBrowserError("Connection refused")

        with (
            patch.object(sd_farm, "discover_wsl_windows_host_ip", return_value="10.255.255.254"),
            patch.object(sd_farm, "_ping_ixbrowser_base", side_effect=fake_ping),
        ):
            status = sd_farm.probe_ixbrowser_bases(
                sd_farm.ixbrowser_api_candidates(
                    use_docker=True,
                    configured_base="http://host.docker.internal:53200/api/v2/",
                ),
                use_docker=True,
            )
        self.assertTrue(status["ok"])
        self.assertEqual(status["ixBrowserApiBase"], "http://10.255.255.254:53200/api/v2/")
        self.assertGreater(len(calls), 1)

    def test_ixbrowser_api_candidates_include_wsl_ip(self):
        with patch.object(sd_farm, "discover_wsl_windows_host_ip", return_value="10.255.255.254"):
            candidates = sd_farm.ixbrowser_api_candidates(
                use_docker=True,
                configured_base="http://host.docker.internal:53200/api/v2/",
            )
        self.assertIn("http://host.docker.internal:53200/api/v2/", candidates)
        self.assertIn("http://10.255.255.254:53200/api/v2/", candidates)

    def test_build_payload_reads_imported_database(self):
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "accounts.sqlite"
            db = Path(tmp) / "source" / "DB" / "data" / "accounts.sqlite"
            _create_accounts_db(db)
            sd_farm.save_imported_accounts_db(db.read_bytes(), dest)
            state = {
                "lock": threading.Lock(),
                "config_path": BACKEND_DIR / "openvpn-proxy-config.example.json",
                "auth_runtime_config": {
                    "sdFarmSource": "import",
                    "sdFarmRoot": r"D:\WORK\SD Farm",
                },
                "use_docker": True,
            }
            with (
                patch.object(sd_farm, "IMPORTED_DB_PATH", dest),
                patch.object(gateway, "IMPORTED_DB_PATH", dest),
                patch.object(gateway, "load_disk_config_expanded", return_value=({"locations": []}, None, 200)),
                patch.object(gateway, "merge_expanded_locations_from_disk", side_effect=lambda cfg, _docker: cfg),
                patch.object(gateway, "list_allowed_ovpn_files", return_value=[]),
                patch.object(gateway, "_probe_ixbrowser", return_value={"ok": True, "ixBrowserApiBase": "http://127.0.0.1:53200/api/v2/", "ixBrowserError": "", "ixBrowserProfileCount": 0}),
                patch.object(gateway, "fetch_ixbrowser_profiles", return_value=[]),
            ):
                payload, err, status = gateway._build_sd_farm_payload(state)
            self.assertIsNone(err)
            self.assertEqual(status, 200)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["root"], r"D:\WORK\SD Farm")
            self.assertEqual(payload["sdFarmSource"], "import")
            self.assertEqual(payload["accountCount"], 1)

    def test_upsert_updates_existing_external_id_route(self):
        state = {
            "lock": threading.Lock(),
            "auth_routes": [
                {
                    "index": 0,
                    "username": "old_name",
                    "label": "Old",
                    "externalId": "61560173093090",
                    "proxyType": "http",
                    "rotationIntervalMinutes": 0,
                    "rotationCountry": "",
                    "rotationLastRun": 0.0,
                    "enabled": True,
                    "egress": {"type": "ovpn", "ovpn": "NC/old.ovpn"},
                }
            ],
            "config_path": BACKEND_DIR / "openvpn-proxy-config.example.json",
            "auth_runtime_config": {},
            "auth_http_port": 58680,
            "auth_socks_port": 58681,
        }
        rows = [
            {
                "uid": "61560173093090",
                "name": "Ali Khan",
                "matchedOvpn": "NC/NCVPN-US-Phoenix-UDP.ovpn",
                "valid": True,
            }
        ]

        with (
            patch.object(gateway, "_persist_auth_routes_config", return_value=None) as persist_mock,
            patch.object(gateway, "_stop_auth_route_backends", return_value=True) as stop_mock,
            patch.object(gateway, "_ixbrowser_proxy_type_from_state", return_value="socks5"),
        ):
            routes, err, changed = gateway._upsert_sd_farm_auth_routes(state, rows)

        self.assertIsNone(err)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["username"], "sd_61560173093090")
        self.assertEqual(routes[0]["proxyType"], "socks5")
        self.assertEqual(routes[0]["externalId"], "61560173093090")
        self.assertEqual(routes[0]["egress"]["ovpn"], "NC/NCVPN-US-Phoenix-UDP.ovpn")
        self.assertIn("sd_61560173093090", changed)
        persist_mock.assert_called_once()
        stop_mock.assert_any_call(state, "old_name", "both")

    def test_upsert_uses_custom_route_username(self):
        state = {
            "lock": threading.Lock(),
            "auth_routes": [],
            "config_path": BACKEND_DIR / "openvpn-proxy-config.example.json",
            "auth_runtime_config": {},
            "auth_http_port": 58680,
            "auth_socks_port": 58681,
        }
        rows = [
            {
                "uid": "61560173093090",
                "name": "Ali Khan",
                "routeUsername": "rose_selma",
                "matchedOvpn": "NC/NCVPN-US-Phoenix-UDP.ovpn",
                "valid": True,
            }
        ]
        with (
            patch.object(gateway, "_persist_auth_routes_config", return_value=None),
            patch.object(gateway, "_stop_auth_route_backends", return_value=True),
            patch.object(gateway, "_ixbrowser_proxy_type_from_state", return_value="http"),
        ):
            routes, err, _changed = gateway._upsert_sd_farm_auth_routes(state, rows)
        self.assertIsNone(err)
        self.assertEqual(routes[0]["username"], "rose_selma")


if __name__ == "__main__":
    unittest.main()
