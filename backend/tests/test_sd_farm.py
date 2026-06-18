import json
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


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

    def test_matches_sd_farm_openvpn_to_portico_ovpn_file(self):
        matched, err = sd_farm.match_ovpn(
            "NCVPN-US-Phoenix-UDP",
            ["NC/NCVPN-US-Phoenix-UDP.ovpn", "NC/NCVPN-US-NewYork-UDP.ovpn"],
        )

        self.assertEqual(matched, "NC/NCVPN-US-Phoenix-UDP.ovpn")
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
        self.assertEqual(calls["payload"]["profile_id"], "99")
        self.assertEqual(calls["payload"]["proxy_info"]["proxy_mode"], 2)
        self.assertEqual(calls["payload"]["proxy_info"]["proxy_user"], "sd_99")


class SDFarmGatewaySyncTests(unittest.TestCase):
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
        ):
            routes, err, changed = gateway._upsert_sd_farm_auth_routes(state, rows)

        self.assertIsNone(err)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["username"], "sd_61560173093090")
        self.assertEqual(routes[0]["externalId"], "61560173093090")
        self.assertEqual(routes[0]["egress"]["ovpn"], "NC/NCVPN-US-Phoenix-UDP.ovpn")
        self.assertIn("sd_61560173093090", changed)
        persist_mock.assert_called_once()
        stop_mock.assert_any_call(state, "old_name", "both")


if __name__ == "__main__":
    unittest.main()
