"""BiBit 网关入口路径：平台固定的 /bibit/v1/invoke 必须可用，/invoke 作为兼容入口保留。"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python" / "src"))

from bianbu_agent_sdk.memory.bibit_adapter import INVOKE_PATH, AdapterServer  # noqa: E402


class _StubState:
    def invoke(self, operation: str, params: dict) -> dict:
        return {"operation": operation, "params": params}


class AdapterPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = AdapterServer(("127.0.0.1", 0), _StubState(), "*")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post(self, path: str) -> tuple[int, dict]:
        body = json.dumps(
            {
                "protocolVersion": 1,
                "requestId": "test-1",
                "serviceId": "memory",
                "operationId": "privacy.settings.get",
                "params": {},
            }
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method="POST"
        )
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_platform_invoke_path_serves_requests(self) -> None:
        status, payload = self.post(INVOKE_PATH)
        self.assertEqual(status, 200)
        self.assertIs(payload.get("ok"), True)
        self.assertEqual(payload.get("requestId"), "test-1")

    def test_legacy_invoke_path_still_serves_requests(self) -> None:
        status, payload = self.post("/invoke")
        self.assertEqual(status, 200)
        self.assertIs(payload.get("ok"), True)

    def test_unknown_path_is_rejected(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/missing", data=b"{}", method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(raised.exception.code, 404)

    def test_discovery_advertises_platform_path(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload.get("invokePath"), INVOKE_PATH)


if __name__ == "__main__":
    unittest.main()
