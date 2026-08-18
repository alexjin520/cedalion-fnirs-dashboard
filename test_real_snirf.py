"""Regression checks against the bundled Cedalion SNIRF recordings."""

import hashlib
import math
import os
import unittest
from pathlib import Path

from server import AnalysisConfig, AnalysisData, load_analysis


_configured_sample_dir = os.getenv("FNIRS_REAL_SAMPLE_DIR")
if _configured_sample_dir is None:
    DATA_DIR = Path(__file__).resolve().parent / "data" / "samples"
    REQUIRE_SAMPLES = False
else:
    DATA_DIR = Path(_configured_sample_dir).expanduser().resolve()
    REQUIRE_SAMPLES = True
SAMPLE_NAMES = (
    "fingertapping.snirf",
    "mne_nirsport2_raw.snirf",
    "recording_20260419_173115.snirf",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class BundledSnirfRegressionTests(unittest.TestCase):
    """Keep the scientific status contract stable without full numeric snapshots."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = {name: DATA_DIR / name for name in SAMPLE_NAMES}
        missing = [name for name, path in cls.paths.items() if not path.is_file()]
        if missing:
            message = "缺少真实 SNIRF 样例：" + ", ".join(missing)
            if REQUIRE_SAMPLES:
                raise AssertionError(
                    f"FNIRS_REAL_SAMPLE_DIR={DATA_DIR} 中 {message}"
                )
            raise unittest.SkipTest(message)

        cls.source_hashes = {
            name: sha256_file(path) for name, path in cls.paths.items()
        }
        cls.analyses: dict[str, AnalysisData] = {
            name: load_analysis(
                path,
                config=AnalysisConfig(),
                recording_id=name,
            )
            for name, path in cls.paths.items()
        }
        cls.after_hashes = {
            name: sha256_file(path) for name, path in cls.paths.items()
        }

    def test_analysis_does_not_modify_any_bundled_source_file(self) -> None:
        for name in SAMPLE_NAMES:
            self.assertEqual(self.source_hashes[name], self.after_hashes[name], name)
            summary = self.analyses[name].summary
            self.assertEqual(summary["analysis"]["input_sha256"], self.source_hashes[name])
            self.assertFalse(summary["input_validation"]["compatibility"]["source_file_modified"])

    def test_each_sample_has_a_complete_seconds_and_resampling_audit(self) -> None:
        for name in SAMPLE_NAMES:
            summary = self.analyses[name].summary
            audit = summary["resampling"]
            self.assertEqual(audit["mode"], "auto", name)
            self.assertFalse(audit["applied"], name)
            self.assertTrue(audit["source_sampling_is_uniform"], name)
            self.assertTrue(audit["target_sampling_is_uniform"], name)
            self.assertEqual(audit["source_samples"], summary["samples"], name)
            self.assertEqual(audit["target_samples"], summary["samples"], name)
            self.assertEqual(audit["interpolated_samples"], 0, name)
            self.assertFalse(audit["anti_aliasing"]["applied"], name)
            self.assertEqual(
                audit["event_timing"]["policy"],
                "preserve_onset_duration_seconds",
                name,
            )
            self.assertEqual(
                audit["event_timing"]["events"],
                summary["stimulus_events"],
                name,
            )
            self.assertEqual(
                summary["input_validation"]["time_normalisation"]["analysis_time_unit"],
                "second",
                name,
            )
            self.assertTrue(math.isfinite(summary["sample_rate_hz"]), name)
            self.assertGreater(summary["sample_rate_hz"], 0.0, name)
            self.assertGreater(summary["samples"], 1, name)
            self.assertGreater(summary["channels"], 0, name)

    def test_fingertapping_is_ready_with_repeated_conditions(self) -> None:
        analysis = self.analyses["fingertapping.snirf"]
        summary = analysis.summary
        readiness = analysis.task_summary["inference_readiness"]
        epoch_selection = analysis.task_summary["epoch_selection"]

        self.assertEqual(summary["subject"]["display_name"], "P1")
        self.assertTrue(analysis.task_summary["available"])
        self.assertTrue(analysis.glm_summary["available"])
        self.assertEqual(readiness["state"], "ready")
        self.assertTrue(readiness["inference_ready"])
        self.assertEqual(len(readiness["condition_counts"]), 3)
        self.assertTrue(
            all(item["usable_count"] >= 2 for item in readiness["condition_counts"])
        )
        self.assertEqual(epoch_selection["requested_events"], 90)
        self.assertEqual(epoch_selection["included_events"], 90)
        self.assertEqual(epoch_selection["excluded_boundary_events"], 0)

    def test_short_sample_reports_task_and_glm_unavailable(self) -> None:
        analysis = self.analyses["mne_nirsport2_raw.snirf"]
        readiness = analysis.task_summary["inference_readiness"]

        self.assertEqual(analysis.summary["stimulus_events"], 0)
        self.assertFalse(analysis.task_summary["available"])
        self.assertFalse(analysis.glm_summary["available"])
        self.assertEqual(readiness["state"], "unavailable")
        self.assertFalse(readiness["inference_ready"])
        self.assertEqual(analysis.task_summary["epoch_selection"]["requested_events"], 0)

    def test_recording_sample_preserves_boundary_event_audit(self) -> None:
        analysis = self.analyses["recording_20260419_173115.snirf"]
        summary = analysis.summary
        task = analysis.task_summary
        epoch_selection = task["epoch_selection"]

        self.assertEqual(summary["subject"]["display_name"], "subject-001")
        self.assertTrue(task["available"])
        self.assertTrue(analysis.glm_summary["available"])
        self.assertEqual(task["inference_readiness"]["state"], "exploratory")
        self.assertFalse(task["inference_readiness"]["inference_ready"])
        self.assertEqual(epoch_selection["requested_events"], 4)
        self.assertEqual(epoch_selection["included_events"], 3)
        self.assertEqual(epoch_selection["excluded_boundary_events"], 1)
        self.assertEqual(epoch_selection["excluded_incomplete_baseline"], 1)
        self.assertEqual(epoch_selection["excluded_incomplete_post_window"], 0)
        self.assertEqual(summary["raw_channels"], 1728)
        self.assertEqual(summary["analyzed_channels"], 239)


if __name__ == "__main__":
    unittest.main()
