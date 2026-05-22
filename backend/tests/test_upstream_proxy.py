import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import gateway  # noqa: E402
from upstream_proxy import (  # noqa: E402
    UpstreamProxyError,
    import_proxy_lines,
    normalize_profile,
    parse_proxy_line,
    public_profile,
)


class UpstreamProxyCatalogTests(unittest.TestCase):
    def test_profile_validation_rejects_invalid_scheme_and_port(self):
        with self.assertRaises(UpstreamProxyError):
            normalize_profile({"scheme": "https", "host": "proxy.example.com", "port": 9000})
        with self.assertRaises(UpstreamProxyError):
            normalize_profile({"scheme": "http", "host": "proxy.example.com", "port": 70000})

    def test_parse_colon_and_url_formats(self):
        colon = parse_proxy_line("proxy.example.com:9000:user:pass")
        self.assertEqual(colon["scheme"], "http")
        self.assertEqual(colon["host"], "proxy.example.com")
        self.assertEqual(colon["port"], 9000)
        self.assertEqual(colon["username"], "user")
        self.assertEqual(colon["password"], "pass")

        url = parse_proxy_line("socks5://url-user:url-pass@socks.example.com:1080")
        self.assertEqual(url["scheme"], "socks5")
        self.assertEqual(url["host"], "socks.example.com")
        self.assertEqual(url["port"], 1080)
        self.assertEqual(url["username"], "url-user")
        self.assertEqual(url["password"], "url-pass")

    def test_import_skips_blank_lines_and_reports_line_errors(self):
        profiles, results = import_proxy_lines("\nproxy.example.com:9000\nbad-line\n")
        self.assertEqual(len(profiles), 1)
        self.assertEqual([row["line"] for row in results], [2, 3])
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])

    def test_public_profile_masks_password_and_update_preserves_omitted_password(self):
        original = normalize_profile(
            {
                "id": "proxy-1",
                "scheme": "http",
                "host": "proxy.example.com",
                "port": 9000,
                "username": "user",
                "password": "secret",
            },
            allow_new_id=False,
        )
        public = public_profile(original)
        self.assertTrue(public["hasPassword"])
        self.assertNotIn("password", public)

        updated = normalize_profile({"id": "proxy-1", "label": "Renamed"}, existing=original)
        self.assertEqual(updated["password"], "secret")
        self.assertEqual(updated["label"], "Renamed")


class TypedEgressStateTests(unittest.TestCase):
    def test_legacy_assignment_loads_as_ovpn_egress(self):
        raw = {"version": 1, "assignments": {"50000": "NC/example.ovpn"}, "activePorts": [50000]}
        parsed = gateway._ingest_assignments_raw(
            raw,
            50000,
            1,
            {"ovpnRoot": "../missing"},
            BACKEND_DIR / "openvpn-proxy-config.example.json",
            False,
            "test",
        )
        assignments = parsed[0]
        egress = parsed[7]
        self.assertEqual(assignments[50000], "NC/example.ovpn")
        self.assertEqual(egress[50000], {"type": "ovpn", "ovpn": "NC/example.ovpn"})

    def test_state_payload_keeps_upstream_reference_outside_legacy_assignments(self):
        payload = gateway.assignments_state_payload(
            {},
            [50001],
            egress_by_port={50001: {"type": "upstream", "upstreamProxyId": "proxy-abc"}},
            upstream_refresh_intervals={50001: 15},
            upstream_refresh_last_run={50001: 42.0},
        )
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["assignments"], {})
        self.assertEqual(
            payload["egress"]["50001"],
            {"type": "upstream", "upstreamProxyId": "proxy-abc"},
        )
        self.assertEqual(payload["upstreamRefreshIntervals"]["50001"], 15)


class UpstreamLifecycleTests(unittest.TestCase):
    @staticmethod
    def _profile(profile_id):
        return normalize_profile(
            {
                "id": profile_id,
                "scheme": "http",
                "host": "proxy.example.com",
                "port": 9000,
            },
            allow_new_id=False,
        )

    def test_active_upstream_switch_marks_port_starting_with_new_profile(self):
        port = 50000
        state = {
            "port_base": port,
            "locations": [{}],
            "use_docker": False,
            "lock": threading.Lock(),
            "active_ports": {port},
            "activation_state_by_port": {port: "active"},
            "activation_error_by_port": {},
            "activation_cancelled_ports": set(),
            "port_to_slot": {},
            "port_ovpn_assignment": {},
            "port_egress_by_port": {port: {"type": "upstream", "upstreamProxyId": "proxy-old"}},
            "upstream_profiles_by_id": {
                "proxy-old": self._profile("proxy-old"),
                "proxy-new": self._profile("proxy-new"),
            },
        }
        started_threads = []

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None):
                started_threads.append({"target": target, "args": args, "daemon": daemon, "started": False})

            def start(self):
                started_threads[-1]["started"] = True

        with (
            patch.object(gateway.threading, "Thread", FakeThread),
            patch.object(gateway, "persist_assignments_snapshot"),
        ):
            err = gateway._perform_port_egress_change_to(
                state,
                port,
                {"type": "upstream", "upstreamProxyId": "proxy-new"},
                {"locations": [{}]},
                BACKEND_DIR / "openvpn-proxy-config.example.json",
            )

        self.assertIsNone(err)
        self.assertEqual(state["port_egress_by_port"][port]["upstreamProxyId"], "proxy-new")
        self.assertEqual(state["activation_state_by_port"][port], "starting")
        self.assertIn(port, state["active_ports"])
        self.assertTrue(started_threads[0]["started"])

    def test_refresh_restarts_only_active_upstream_ports(self):
        now = time.time()
        state = {
            "config_path": BACKEND_DIR / "openvpn-proxy-config.example.json",
            "use_docker": False,
            "lock": threading.Lock(),
            "upstream_refresh_intervals_by_port": {50000: 1, 50001: 1, 50002: 1},
            "upstream_refresh_last_run_by_port": {50000: now - 61, 50001: now - 61, 50002: now - 61},
            "activation_state_by_port": {50000: "active", 50001: "inactive", 50002: "active"},
            "port_egress_by_port": {
                50000: {"type": "upstream", "upstreamProxyId": "proxy-a"},
                50001: {"type": "upstream", "upstreamProxyId": "proxy-b"},
                50002: {"type": "ovpn", "ovpn": "NC/example.ovpn"},
            },
        }
        restarted = []
        sleep_calls = 0
        previous_shutdown_flag = gateway.shutdown_flag

        def fake_sleep(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                gateway.shutdown_flag = True

        def fake_change(_state, port, _egress, _runtime_config, _config_path):
            restarted.append(port)
            return None

        gateway.shutdown_flag = False
        try:
            with (
                patch.object(gateway.time, "sleep", side_effect=fake_sleep),
                patch.object(
                    gateway,
                    "load_disk_config_expanded",
                    return_value=({"locations": [{}, {}, {}]}, None, 200),
                ),
                patch.object(gateway, "merge_expanded_locations_from_disk", side_effect=lambda config, *_: config),
                patch.object(gateway, "_perform_port_egress_change_to", side_effect=fake_change),
                patch.object(gateway, "persist_assignments_snapshot"),
            ):
                gateway.upstream_refresh_loop(state)
        finally:
            gateway.shutdown_flag = previous_shutdown_flag

        self.assertEqual(restarted, [50000])


if __name__ == "__main__":
    unittest.main()
