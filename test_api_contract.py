"""In-process HTTP contract checks against a real analysis recording."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from server import (
    AnalysisConfig,
    AnalysisConfigStore,
    DashboardHandler,
    QcDecisionStore,
)
from http.server import ThreadingHTTPServer


DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data" / "samples"
RECORDING_NAME = "recording_20260419_173115.snirf"


class RealRecordingApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        data_dir = Path(
            os.getenv("FNIRS_API_CONTRACT_DATA_DIR", DEFAULT_DATA_DIR)
        ).expanduser().resolve()
        data_file = data_dir / RECORDING_NAME
        if not data_file.is_file():
            raise unittest.SkipTest(f"缺少 API 契约样例：{data_file}")

        cls._runtime = tempfile.TemporaryDirectory(prefix="fnirs-api-contract-")
        runtime = Path(cls._runtime.name)
        config = AnalysisConfig()
        DashboardHandler.data_dir = data_dir
        DashboardHandler.default_data_file = data_file
        DashboardHandler.upload_dir = data_dir / "username"
        DashboardHandler.max_upload_bytes = 1024 * 1024
        DashboardHandler.environment_config = config
        DashboardHandler.analysis_config = config
        DashboardHandler.analysis_config_source = "测试"
        DashboardHandler.analysis_config_store = AnalysisConfigStore(
            runtime / "settings.json", config
        )
        DashboardHandler.qc_decision_store = QcDecisionStore(runtime / "qc.json")
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls._thread = threading.Thread(
            target=cls._httpd.serve_forever,
            name="fnirs-api-contract",
            daemon=True,
        )
        cls._thread.start()
        cls._base_url = f"http://127.0.0.1:{cls._httpd.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "_httpd"):
            cls._httpd.shutdown()
            cls._httpd.server_close()
            cls._thread.join(timeout=5)
        if hasattr(cls, "_runtime"):
            cls._runtime.cleanup()

    @classmethod
    def get_json(cls, path: str) -> dict:
        with urlopen(cls._base_url + path, timeout=90) as response:
            if response.status != 200:
                raise AssertionError(f"{path} returned HTTP {response.status}")
            return json.load(response)

    @classmethod
    def get_bytes(cls, path: str) -> tuple[bytes, str]:
        with urlopen(cls._base_url + path, timeout=90) as response:
            if response.status != 200:
                raise AssertionError(f"{path} returned HTTP {response.status}")
            return response.read(), response.headers.get("Content-Type", "")

    def test_health_and_settings_contract(self) -> None:
        health = self.get_json("/api/health")
        self.assertTrue(health["ok"])

        settings = self.get_json("/api/settings")
        self.assertTrue(settings["ok"])
        for name in (
            "resampling_mode",
            "resampling_target_rate_hz",
            "resampling_max_gap_seconds",
        ):
            self.assertIn(name, settings["settings"])

    def test_recording_and_manifest_contract(self) -> None:
        recording = self.get_json("/api/recording")
        summary = recording["summary"]
        self.assertEqual(
            summary["analysis"]["protocol_version"],
            "snirf-od-hb-cbsi-psp-qc-task-glm-gvtd-censor-resample-v8",
        )
        self.assertEqual(summary["analysis"]["manifest_version"], "1.4")
        self.assertIn("resampling", summary)
        self.assertIn("resampling", summary["input_validation"])
        self.assertIn("inference_readiness", recording["task"])

        manifest = self.get_json("/api/analysis-metadata")
        self.assertEqual(manifest["manifest_version"], "1.4")
        self.assertEqual(manifest["preprocessing"]["resampling"], summary["resampling"])

    def test_probe_contract_includes_positions_and_channel_links(self) -> None:
        probe = self.get_json("/api/probe")
        positions = probe["geometry"]["optode_positions_mm"]
        self.assertGreater(len(positions), 0)
        self.assertTrue(all({"label", "kind", "x_mm", "y_mm", "z_mm"} <= set(item) for item in positions))
        channels = probe["channels"]
        self.assertGreater(len(channels), 0)
        self.assertTrue(all({"source", "detector", "passed"} <= set(item) for item in channels))

    def test_csv_exports_keep_analysis_and_readiness_contract(self) -> None:
        quality = self.get_json("/api/quality")
        self.assertIn("distance_mm", quality["channels"][0])
        self.assertIsNotNone(quality["channels"][0]["distance_mm"])
        channel = next(item["index"] for item in quality["channels"] if item["passed"])
        task_body, task_type = self.get_bytes(f"/api/task-export?channel={channel}")
        self.assertIn("text/csv", task_type)
        task_csv = task_body.decode("utf-8-sig")
        self.assertIn("analysis_id", task_csv.splitlines()[0])
        self.assertIn("inference_state", task_csv.splitlines()[0])

        glm_body, glm_type = self.get_bytes("/api/task-glm-export")
        self.assertIn("text/csv", glm_type)
        glm_csv = glm_body.decode("utf-8-sig")
        self.assertIn("inference_reason", glm_csv.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
