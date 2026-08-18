"""Optional CI smoke test using Cedalion's checksum-verified public SNIRF."""

import os
import hashlib
import unittest
from pathlib import Path

from server import AnalysisConfig, load_analysis


class OfficialSnirfSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.getenv("FNIRS_CI_OFFICIAL_SNIRF") != "1":
            raise unittest.SkipTest("未启用官方 SNIRF CI smoke test")
        configured_path = os.getenv("FNIRS_CI_OFFICIAL_SNIRF_PATH")
        if configured_path:
            cls.path = Path(configured_path).expanduser().resolve()
            if not cls.path.is_file():
                raise AssertionError(f"官方 SNIRF 路径不存在：{cls.path}")
        else:
            import cedalion.data

            cls.path = cedalion.data.DATASETS.fetch("mne_nirsport2_raw.snirf")
        digest = hashlib.sha256(cls.path.read_bytes()).hexdigest()
        cls.assert_hash = "12e5fabe64ecc7ef4b83f6bcd77abb41f5480d5f17a2b1aae0e2ad0406670944"
        if digest != cls.assert_hash:
            raise AssertionError(
                f"官方 SNIRF SHA-256 不匹配：{digest} != {cls.assert_hash}"
            )
        cls.analysis = load_analysis(
            cls.path,
            config=AnalysisConfig(),
            recording_id="mne_nirsport2_raw.snirf",
        )

    def test_public_recording_produces_a_valid_audited_summary(self) -> None:
        summary = self.analysis.summary
        self.assertEqual(summary["filename"], "mne_nirsport2_raw.snirf")
        self.assertGreater(summary["samples"], 1)
        self.assertGreater(summary["channels"], 0)
        self.assertEqual(summary["input_validation"]["time_normalisation"]["analysis_time_unit"], "second")
        self.assertIn("resampling", summary["input_validation"])
        self.assertEqual(summary["resampling"]["mode"], "auto")
        self.assertFalse(self.analysis.task_summary["available"])


if __name__ == "__main__":
    unittest.main()
