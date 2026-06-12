import sys
import threading
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import gateway  # noqa: E402
from storage import DEFAULT_CONFIG  # noqa: E402


class FakeStore:
    def __init__(self):
        self.config = dict(DEFAULT_CONFIG)
        self.assignment_payload = {
            "version": 2,
            "assignments": {"50000": "NC/example.ovpn"},
            "egress": {"50000": {"type": "ovpn", "ovpn": "NC/example.ovpn"}},
            "activePorts": [50000],
            "launcherIds": {"50000": "user-1"},
            "proxyTypes": {"50000": "socks5"},
            "rotationIntervals": {"50000": 15},
            "rotationCountries": {"50000": "US"},
            "rotationLastRun": {"50000": 123.5},
            "upstreamRefreshIntervals": {"50000": 20},
            "upstreamRefreshLastRun": {"50000": 456.5},
        }
        self.saved_payload = None

    def load_config(self):
        return dict(self.config)

    def load_assignment_payload(self, port_base, num_ports):
        return dict(self.assignment_payload)

    def save_assignment_payload(self, payload, port_base, num_ports):
        self.saved_payload = payload


class PostgresStorageIntegrationTests(unittest.TestCase):
    def tearDown(self):
        gateway.DB_STORE = None

    def test_load_config_uses_store_without_config_file(self):
        gateway.DB_STORE = FakeStore()
        config, err, status = gateway.load_disk_config_expanded(Path("missing.json"))
        self.assertIsNone(err)
        self.assertEqual(status, 200)
        self.assertEqual(config["portBase"], 50000)
        self.assertIn("locations", config)

    def test_load_assignments_uses_store_payload(self):
        gateway.DB_STORE = FakeStore()
        loaded = gateway.load_gateway_assignments_state(
            Path("missing-assignments.json"),
            "",
            "",
            50000,
            1,
            {"ovpnRoot": ".", "locations": [{}]},
            Path("config.json"),
            False,
        )
        self.assertEqual(loaded[0], {50000: "NC/example.ovpn"})
        self.assertEqual(loaded[1], [50000])
        self.assertEqual(loaded[2], {50000: "user-1"})
        self.assertEqual(loaded[3], {50000: "socks5"})
        self.assertEqual(loaded[4], {50000: 15})
        self.assertEqual(loaded[8], {50000: 20})

    def test_persist_assignments_writes_store_payload(self):
        store = FakeStore()
        gateway.DB_STORE = store
        state = {
            "lock": threading.Lock(),
            "port_base": 50000,
            "num_ports": 1,
            "port_ovpn_assignment": {50000: "NC/example.ovpn"},
            "port_egress_by_port": {50000: {"type": "ovpn", "ovpn": "NC/example.ovpn"}},
            "active_ports": {50000},
            "launcher_ids_by_port": {50000: "user-1"},
            "proxy_types_by_port": {50000: "socks5"},
            "rotation_intervals_by_port": {50000: 15},
            "rotation_countries_by_port": {50000: "US"},
            "rotation_last_run_by_port": {50000: 123.5},
            "upstream_refresh_intervals_by_port": {50000: 20},
            "upstream_refresh_last_run_by_port": {50000: 456.5},
            "db_store": store,
        }
        gateway.persist_assignments_snapshot(state)
        self.assertEqual(store.saved_payload["assignments"], {"50000": "NC/example.ovpn"})
        self.assertEqual(store.saved_payload["activePorts"], [50000])
        self.assertEqual(store.saved_payload["proxyTypes"], {"50000": "socks5"})
        self.assertEqual(store.saved_payload["upstreamRefreshIntervals"], {"50000": 20})


if __name__ == "__main__":
    unittest.main()
