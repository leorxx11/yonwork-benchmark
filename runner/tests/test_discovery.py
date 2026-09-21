from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner.discovery import (
    DiscoveryError,
    ENV_BASE_URL,
    ENV_RUNTIME_FILE,
    ENV_RUNTIME_HOST,
    ENV_TOKEN,
    discover,
    load_runtime_file,
)


class RuntimeFileTests(unittest.TestCase):
    def _runtime_file(self, payload: object) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "host-api-runtime.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_reads_port_and_token(self) -> None:
        path = self._runtime_file(
            {"port": 4123, "token": "abc", "pid": 6568, "version": "1.0.8"}
        )
        with mock.patch.dict(os.environ, {ENV_RUNTIME_FILE: str(path)}, clear=False):
            os.environ.pop(ENV_BASE_URL, None)
            endpoint = discover()
        # 端口来自文件，不是硬编码的 3211（被占用时会回落随机端口）。
        self.assertEqual("http://127.0.0.1:4123", endpoint.base_url)
        self.assertEqual("abc", endpoint.token)
        self.assertEqual(6568, endpoint.pid)

    def test_token_never_shows_up_in_repr(self) -> None:
        path = self._runtime_file({"port": 3211, "token": "secret-token"})
        with mock.patch.dict(os.environ, {ENV_RUNTIME_FILE: str(path)}, clear=False):
            os.environ.pop(ENV_BASE_URL, None)
            endpoint = discover()
        self.assertNotIn("secret-token", repr(endpoint))

    def test_runtime_host_can_be_overridden_for_container_networking(self) -> None:
        path = self._runtime_file({"port": 4123, "token": "abc"})
        with mock.patch.dict(
            os.environ,
            {
                ENV_RUNTIME_FILE: str(path),
                ENV_RUNTIME_HOST: "host.docker.internal",
            },
            clear=False,
        ):
            os.environ.pop(ENV_BASE_URL, None)
            endpoint = discover()
        self.assertEqual("http://host.docker.internal:4123", endpoint.base_url)

    def test_rejects_missing_token(self) -> None:
        path = self._runtime_file({"port": 3211})
        with self.assertRaisesRegex(DiscoveryError, "token"):
            load_runtime_file(path)

    def test_rejects_bad_port(self) -> None:
        path = self._runtime_file({"port": 0, "token": "abc"})
        with self.assertRaisesRegex(DiscoveryError, "port"):
            load_runtime_file(path)

    def test_missing_file_is_a_clear_error(self) -> None:
        with self.assertRaisesRegex(DiscoveryError, "找不到运行时文件"):
            load_runtime_file(Path("/nonexistent/host-api-runtime.json"))

    def test_env_url_wins(self) -> None:
        with mock.patch.dict(
            os.environ,
            {ENV_BASE_URL: "http://127.0.0.1:9999/", ENV_TOKEN: "t"},
            clear=False,
        ):
            endpoint = discover()
        self.assertEqual("http://127.0.0.1:9999", endpoint.base_url)
        self.assertEqual("http://127.0.0.1:9999/api/chat/send", endpoint.url("/api/chat/send"))


if __name__ == "__main__":
    unittest.main()
