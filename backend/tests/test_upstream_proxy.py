import sys
import base64
import errno
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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
    save_catalog,
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

    def test_save_catalog_falls_back_when_bind_mount_rejects_replace(self):
        profile = normalize_profile(
            {"id": "proxy-1", "scheme": "http", "host": "proxy.example.com", "port": 9000},
            allow_new_id=False,
        )
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "upstream-proxy-catalog.json"
            path.write_text('{"version":1,"proxies":[]}\n', encoding="utf-8")
            original_replace = Path.replace

            def busy_replace(self, target):
                if self == path.parent / (path.name + ".tmp") and target == path:
                    raise OSError(errno.EBUSY, "Device or resource busy")
                return original_replace(self, target)

            with patch.object(Path, "replace", busy_replace):
                save_catalog(path, [profile])

            text = path.read_text(encoding="utf-8")
            self.assertIn('"id": "proxy-1"', text)
            self.assertFalse((path.parent / (path.name + ".tmp")).exists())


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


class AuthRoutingTests(unittest.TestCase):
    def test_http_basic_auth_parses_username_and_password(self):
        token = base64.b64encode(b"us_chicago:secret").decode("ascii")
        raw = (
            f"CONNECT example.com:443 HTTP/1.1\r\n"
            f"Host: example.com:443\r\n"
            f"Proxy-Authorization: Basic {token}\r\n\r\n"
        ).encode("ascii")

        username, password, err = gateway.parse_http_proxy_basic_auth(raw)

        self.assertIsNone(err)
        self.assertEqual(username, "us_chicago")
        self.assertEqual(password, "secret")

    def test_http_basic_auth_rejects_missing_header(self):
        username, password, err = gateway.parse_http_proxy_basic_auth(b"GET http://example.com/ HTTP/1.1\r\n\r\n")

        self.assertEqual(username, "")
        self.assertEqual(password, "")
        self.assertIn("Missing", err)

    def test_proxy_authorization_header_is_stripped_before_internal_forward(self):
        token = base64.b64encode(b"route:pass").decode("ascii")
        raw = (
            f"GET http://example.com/ HTTP/1.1\r\n"
            f"Host: example.com\r\n"
            f"Proxy-Authorization: Basic {token}\r\n"
            f"User-Agent: test\r\n\r\n"
        ).encode("ascii")

        stripped = gateway.strip_proxy_authorization_header(raw)

        self.assertNotIn(b"Proxy-Authorization", stripped)
        self.assertIn(b"User-Agent: test", stripped)

    def test_username_selects_enabled_route_with_global_password(self):
        config = {
            "authRouting": {
                "enabled": True,
                "routes": [
                    {
                        "username": "us_chicago",
                        "label": "US Chicago",
                        "enabled": True,
                        "egress": {"type": "ovpn", "ovpn": "NC/example.ovpn"},
                    },
                    {
                        "username": "disabled",
                        "enabled": False,
                        "egress": {"type": "upstream", "upstreamProxyId": "proxy-a"},
                    },
                ],
            }
        }
        state = {"auth_routes": gateway.normalize_auth_routes(config), "auth_global_password": "secret"}

        idx, route, err = gateway._auth_route_for_credentials(state, "us_chicago", "secret")
        self.assertIsNone(err)
        self.assertEqual(idx, 0)
        self.assertEqual(route["egress"], {"type": "ovpn", "ovpn": "NC/example.ovpn"})

        _idx, _route, err = gateway._auth_route_for_credentials(state, "us_chicago", "wrong")
        self.assertIn("Invalid", err)

        _idx, _route, err = gateway._auth_route_for_credentials(state, "disabled", "secret")
        self.assertIn("disabled", err)


class OvpnUploadTests(unittest.TestCase):
    def test_upload_rejects_unsafe_provider_and_filename(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaisesRegex(gateway.OvpnUploadError, "provider"):
                gateway.save_ovpn_upload_batch(
                    root,
                    "../bad",
                    "user",
                    "pass",
                    [{"filename": "ok.ovpn", "data": b"client\n"}],
                )
            with self.assertRaisesRegex(gateway.OvpnUploadError, "filename"):
                gateway.save_ovpn_upload_batch(
                    root,
                    "NC",
                    "user",
                    "pass",
                    [{"filename": "../bad.ovpn", "data": b"client\n"}],
                )

    def test_upload_accepts_loose_files_and_writes_provider_auth(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = gateway.save_ovpn_upload_batch(
                root,
                "NC",
                "vpn-user",
                "vpn-pass",
                [
                    {"filename": "United_States_California_Los_Angeles.ovpn", "data": b"client\nca ca.crt\n"},
                    {"filename": "ca.crt", "data": b"certificate\n"},
                ],
            )

            self.assertEqual(result["uploaded"], 2)
            self.assertEqual(result["ovpnUploaded"], 1)
            self.assertTrue((root / "NC" / "United_States_California_Los_Angeles.ovpn").is_file())
            self.assertEqual((root / "NC" / "auth.txt").read_text(encoding="utf-8"), "vpn-user\nvpn-pass\n")
            files = gateway.list_allowed_ovpn_files(
                {"ovpnRoot": str(root)},
                root / "openvpn-proxy-config.json",
                False,
            )
            self.assertEqual(files, ["NC/United_States_California_Los_Angeles.ovpn"])

    def test_upload_preserves_existing_files_unless_overwrite_is_enabled(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            gateway.save_ovpn_upload_batch(
                root,
                "NC",
                "user",
                "pass",
                [{"filename": "profile.ovpn", "data": b"old\n"}],
            )

            with self.assertRaisesRegex(gateway.OvpnUploadError, "already exists"):
                gateway.save_ovpn_upload_batch(
                    root,
                    "NC",
                    "user",
                    "pass",
                    [{"filename": "profile.ovpn", "data": b"new\n"}],
                )

            gateway.save_ovpn_upload_batch(
                root,
                "NC",
                "user",
                "new-pass",
                [{"filename": "profile.ovpn", "data": b"new\n"}],
                overwrite=True,
            )
            self.assertEqual((root / "NC" / "profile.ovpn").read_bytes(), b"new\n")
            self.assertEqual((root / "NC" / "auth.txt").read_text(encoding="utf-8"), "user\nnew-pass\n")


class OVPNLocationChangeTests(unittest.TestCase):
    @staticmethod
    def _state(port: int, active: bool = False):
        return {
            "port_base": port,
            "locations": [{}],
            "use_docker": False,
            "lock": threading.Lock(),
            "active_ports": {port} if active else set(),
            "activation_state_by_port": {port: "active"} if active else {},
            "activation_error_by_port": {},
            "activation_cancelled_ports": set(),
            "port_to_slot": {},
            "port_ovpn_assignment": {},
            "port_egress_by_port": {},
            "upstream_profiles_by_id": {},
        }

    @staticmethod
    def _runtime_root(tmpdir: str):
        root = Path(tmpdir)
        provider = root / "NC"
        provider.mkdir()
        (provider / "auth.txt").write_text("vpn-user\nvpn-pass\n", encoding="utf-8")
        (provider / "United_States_California_Los_Angeles.ovpn").write_text("client\n", encoding="utf-8")
        (provider / "Germany_Berlin.ovpn").write_text("client\n", encoding="utf-8")
        return root, {"ovpnRoot": str(root), "locations": [{}]}, root / "config.json"

    def test_country_location_change_saves_inactive_port_without_starting(self):
        port = 50000
        with TemporaryDirectory() as tmpdir:
            root, runtime_config, config_path = self._runtime_root(tmpdir)
            state = self._state(port, active=False)
            with patch.object(gateway, "persist_assignments_snapshot"):
                result, err = gateway._perform_port_location_change(
                    state,
                    port,
                    runtime_config,
                    config_path,
                    requested_country="US",
                )

            self.assertIsNone(err)
            self.assertEqual(result["activationState"], "inactive")
            self.assertEqual(
                state["port_ovpn_assignment"][port],
                "NC/United_States_California_Los_Angeles.ovpn",
            )
            self.assertEqual(root.name, Path(tmpdir).name)

    def test_active_exact_location_change_marks_port_starting(self):
        port = 50000
        with TemporaryDirectory() as tmpdir:
            _root, runtime_config, config_path = self._runtime_root(tmpdir)
            state = self._state(port, active=True)
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
                result, err = gateway._perform_port_location_change(
                    state,
                    port,
                    runtime_config,
                    config_path,
                    requested_ovpn="NC/Germany_Berlin.ovpn",
                )

            self.assertIsNone(err)
            self.assertEqual(result["activationState"], "starting")
            self.assertEqual(state["port_egress_by_port"][port], {"type": "ovpn", "ovpn": "NC/Germany_Berlin.ovpn"})
            self.assertEqual(state["activation_state_by_port"][port], "starting")
            self.assertTrue(started_threads[0]["started"])


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
