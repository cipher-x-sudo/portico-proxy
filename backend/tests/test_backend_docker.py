import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import backend_docker  # noqa: E402


class FakeContainer:
    def __init__(self, name="proxy-60001", short_id="abc123"):
        self.name = name
        self.short_id = short_id
        self.stop_calls = 0
        self.remove_calls = 0
        self.on_remove = None

    def stop(self, timeout=5):
        self.stop_calls += 1

    def remove(self, force=True):
        self.remove_calls += 1
        if self.on_remove:
            self.on_remove()


class FakeContainers:
    def __init__(self):
        self.by_name = {}
        self.run_effects = []
        self.run_calls = []
        self.get_calls = []

    def get(self, name):
        self.get_calls.append(name)
        if name not in self.by_name:
            raise Exception("404 Client Error: Not Found")
        return self.by_name[name]

    def run(self, image, **kwargs):
        self.run_calls.append((image, kwargs))
        if self.run_effects:
            effect = self.run_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return FakeContainer(kwargs.get("name", "proxy-60001"), "new123")


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainers()
        self.networks = SimpleNamespace(list=lambda: [SimpleNamespace(name="proxynet")])


class BackendDockerStartTests(unittest.TestCase):
    def _start(self, client):
        docker_module = SimpleNamespace(from_env=lambda: client)
        config = {
            "locations": [{"ovpn": "example.ovpn"}],
            "internalProxyAuthEnabled": False,
        }
        upstream_profile = {"scheme": "http", "host": "upstream.example.com", "port": 8080}
        with patch.dict(sys.modules, {"docker": docker_module}):
            return backend_docker.start_docker_backend(
                0,
                60001,
                config,
                "portico-worker",
                "proxynet",
                "ovpn_data",
                proxy_listen_scheme="socks5",
                upstream_profile=upstream_profile,
            )

    def test_start_removes_existing_worker_before_create(self):
        client = FakeDockerClient()
        stale = FakeContainer("proxy-60001")

        def remove_stale():
            client.containers.by_name.pop("proxy-60001", None)

        stale.on_remove = remove_stale
        client.containers.by_name["proxy-60001"] = stale
        started = FakeContainer("proxy-60001", "new456")
        client.containers.run_effects = [started]

        backend_host, backend_port = self._start(client)

        self.assertEqual((backend_host, backend_port), ("proxy-60001", backend_docker.WORKER_PROXY_PORT))
        self.assertEqual(stale.stop_calls, 1)
        self.assertEqual(stale.remove_calls, 1)
        self.assertEqual(len(client.containers.run_calls), 1)

    def test_start_retries_once_after_container_name_conflict(self):
        client = FakeDockerClient()
        stale = FakeContainer("proxy-60001")

        def remove_stale():
            client.containers.by_name.pop("proxy-60001", None)

        stale.on_remove = remove_stale

        original_get = client.containers.get
        get_count = {"value": 0}

        def get_with_late_stale(name):
            get_count["value"] += 1
            if name == "proxy-60001" and get_count["value"] == 2:
                client.containers.by_name["proxy-60001"] = stale
            return original_get(name)

        client.containers.get = get_with_late_stale
        conflict = Exception(
            '409 Client Error: Conflict ("Conflict. The container name "/proxy-60001" is already in use")'
        )
        started = FakeContainer("proxy-60001", "new789")
        client.containers.run_effects = [conflict, started]

        backend_host, backend_port = self._start(client)

        self.assertEqual((backend_host, backend_port), ("proxy-60001", backend_docker.WORKER_PROXY_PORT))
        self.assertEqual(len(client.containers.run_calls), 2)
        self.assertEqual(stale.remove_calls, 1)

    def test_start_does_not_retry_indefinitely_on_repeated_conflict(self):
        client = FakeDockerClient()
        conflict = Exception(
            '409 Client Error: Conflict ("Conflict. The container name "/proxy-60001" is already in use")'
        )
        client.containers.run_effects = [conflict, conflict]

        with self.assertRaises(Exception) as ctx:
            self._start(client)

        self.assertIs(ctx.exception, conflict)
        self.assertEqual(len(client.containers.run_calls), 2)

    def test_start_propagates_non_conflict_docker_error(self):
        client = FakeDockerClient()
        error = Exception("500 Server Error: Docker daemon exploded")
        client.containers.run_effects = [error]

        with self.assertRaises(Exception) as ctx:
            self._start(client)

        self.assertIs(ctx.exception, error)
        self.assertEqual(len(client.containers.run_calls), 1)


if __name__ == "__main__":
    unittest.main()
