"""API checks for user SNIRF uploads and deletion boundaries."""

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import h5py

from server import AnalysisConfig, AnalysisConfigStore, DashboardHandler, QcDecisionStore


class UserRecordingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._runtime = tempfile.TemporaryDirectory(prefix="fnirs-upload-api-")
        root = Path(cls._runtime.name) / "data"
        samples = root / "samples"
        samples.mkdir(parents=True)
        cls._sample = samples / "builtin.snirf"
        with h5py.File(cls._sample, "w") as handle:
            handle.create_group("nirs")

        DashboardHandler.data_dir = root
        DashboardHandler.default_data_file = cls._sample
        DashboardHandler.upload_dir = root / "username"
        DashboardHandler.max_upload_bytes = 1024 * 1024
        config = AnalysisConfig()
        DashboardHandler.environment_config = config
        DashboardHandler.analysis_config = config
        DashboardHandler.analysis_config_source = "测试"
        DashboardHandler.analysis_config_store = AnalysisConfigStore(
            Path(cls._runtime.name) / "settings.json", config
        )
        DashboardHandler.qc_decision_store = QcDecisionStore(
            Path(cls._runtime.name) / "qc.json"
        )
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()
        cls._base_url = f"http://127.0.0.1:{cls._httpd.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._httpd.shutdown()
        cls._httpd.server_close()
        cls._thread.join(timeout=5)
        cls._runtime.cleanup()

    @classmethod
    def request_json(
        cls,
        path: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        request = Request(
            cls._base_url + path,
            data=body,
            method=method,
            headers=headers or {},
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_upload_is_listed_and_can_be_deleted(self) -> None:
        body = self._sample.read_bytes()
        status, payload = self.request_json(
            "/api/uploads",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-FNIRS-Filename": quote("user-recording.snirf"),
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["recording"]["id"], "username/user-recording.snirf")
        self.assertTrue(payload["recording"]["is_uploaded"])

        status, inventory = self.request_json("/api/recordings")
        self.assertEqual(status, 200)
        uploaded = next(
            item for item in inventory["recordings"] if item["id"] == "username/user-recording.snirf"
        )
        self.assertTrue(uploaded["is_uploaded"])

        status, payload = self.request_json(
            "/api/uploads?recording=username%2Fuser-recording.snirf",
            method="DELETE",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["deleted_recording"], "username/user-recording.snirf")
        self.assertFalse((DashboardHandler.upload_dir / "user-recording.snirf").exists())

    def test_builtin_recordings_cannot_be_deleted(self) -> None:
        status, payload = self.request_json(
            "/api/uploads?recording=samples%2Fbuiltin.snirf",
            method="DELETE",
        )
        self.assertEqual(status, 403)
        self.assertIn("只能删除", payload["error"])
        self.assertTrue(self._sample.exists())

    def test_upload_rejects_non_snirf_and_path_traversal(self) -> None:
        status, payload = self.request_json(
            "/api/uploads",
            method="POST",
            body=b"not a snirf",
            headers={
                "Content-Type": "application/octet-stream",
                "X-FNIRS-Filename": quote("bad.txt"),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn(".snirf", payload["error"])

        status, payload = self.request_json(
            "/api/uploads",
            method="POST",
            body=self._sample.read_bytes(),
            headers={
                "Content-Type": "application/octet-stream",
                "X-FNIRS-Filename": quote("../escape.snirf"),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("不能包含目录", payload["error"])


if __name__ == "__main__":
    unittest.main()
