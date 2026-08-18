import unittest

import numpy as np
import pandas as pd
import xarray as xr

from server import (
    AnalysisConfig,
    _build_task_analysis,
    _filter_complete_task_events,
    _inference_readiness,
    _stimulus_with_durations,
    _validate_recording_input,
)


def amplitudes_with_time(time: np.ndarray) -> xr.DataArray:
    values = np.ones((1, 2, len(time)), dtype=np.float64)
    amplitudes = xr.DataArray(
        values,
        dims=("channel", "wavelength", "time"),
        coords={
            "channel": ["S1D1"],
            "source": ("channel", ["S1"]),
            "detector": ("channel", ["D1"]),
            "wavelength": [760.0, 850.0],
            "time": np.asarray(time, dtype=np.float64),
        },
        name="amplitudes",
    ).pint.quantify("V")
    amplitudes.time.attrs["units"] = "s"
    return amplitudes


class AnalysisConfigurationGuardrailTests(unittest.TestCase):
    def test_rejects_zero_length_pre_stimulus_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "刺激前和刺激后窗口都必须大于 0"):
            AnalysisConfig(epoch_before_seconds=0.0).validate()


class SamplingUniformityGuardrailTests(unittest.TestCase):
    def test_accepts_sampling_interval_deviation_at_one_percent(self) -> None:
        time = np.cumsum([0.0, 0.1, 0.101, 0.1])

        result = _validate_recording_input(
            amplitudes_with_time(time),
            pd.DataFrame(),
            AnalysisConfig(),
        )

        self.assertTrue(result["valid"])
        self.assertTrue(result["sampling_is_uniform"])
        self.assertAlmostEqual(result["maximum_sampling_deviation_fraction"], 0.01)

    def test_rejects_sampling_interval_deviation_over_one_percent(self) -> None:
        time = np.cumsum([0.0, 0.1, 0.102, 0.1])

        with self.assertRaisesRegex(ValueError, "采样间隔不均匀.*必须先重采样"):
            _validate_recording_input(
                amplitudes_with_time(time),
                pd.DataFrame(),
                AnalysisConfig(),
            )


class CompleteTaskEventGuardrailTests(unittest.TestCase):
    def test_normalises_missing_and_invalid_durations_to_instantaneous_events(self) -> None:
        missing = _stimulus_with_durations(
            pd.DataFrame([{"onset": 1.0, "trial_type": "A"}])
        )
        invalid = _stimulus_with_durations(
            pd.DataFrame(
                {
                    "duration": [2.5, -1.0, np.nan, "invalid", pd.NA],
                    "trial_type": ["A"] * 5,
                }
            )
        )

        self.assertEqual(missing["duration"].tolist(), [0.0])
        self.assertEqual(invalid["duration"].tolist(), [2.5, 0.0, 0.0, 0.0, 0.0])

    def test_filters_incomplete_boundaries_and_reports_audit_counts(self) -> None:
        stimulus = pd.DataFrame(
            [
                {"onset": 4.0, "duration": 1.0, "value": 1, "trial_type": "A"},
                {"onset": 5.0, "duration": 1.0, "value": 1, "trial_type": "A"},
                {"onset": 10.0, "duration": 1.0, "value": 1, "trial_type": "A"},
                {"onset": 26.0, "duration": 1.0, "value": 2, "trial_type": "B"},
                {"onset": "invalid", "duration": 1.0, "value": 2, "trial_type": "B"},
                {"onset": 15.0, "duration": 0.0, "value": 9, "trial_type": "Marker"},
            ]
        )

        eligible, audit = _filter_complete_task_events(
            stimulus,
            ["A", "B"],
            np.arange(0.0, 31.0),
            before=5.0,
            after=5.0,
        )

        self.assertEqual(eligible["onset"].tolist(), [5.0, 10.0])
        self.assertEqual(eligible["trial_type"].tolist(), ["A", "A"])
        self.assertEqual(audit["requested_events"], 5)
        self.assertEqual(audit["included_events"], 2)
        self.assertEqual(audit["excluded_boundary_events"], 2)
        self.assertEqual(audit["excluded_incomplete_baseline"], 1)
        self.assertEqual(audit["excluded_incomplete_post_window"], 1)
        self.assertEqual(audit["excluded_invalid_onset"], 1)

        by_condition = {item["value"]: item for item in audit["conditions"]}
        self.assertEqual(
            by_condition["A"],
            {
                "value": "A",
                "label": "A",
                "requested_count": 3,
                "included_count": 2,
                "excluded_incomplete_baseline": 1,
                "excluded_incomplete_post_window": 0,
                "excluded_invalid_onset": 0,
            },
        )
        self.assertEqual(by_condition["B"]["requested_count"], 2)
        self.assertEqual(by_condition["B"]["included_count"], 0)
        self.assertEqual(by_condition["B"]["excluded_incomplete_post_window"], 1)
        self.assertEqual(by_condition["B"]["excluded_invalid_onset"], 1)

    def test_condition_with_only_boundary_events_does_not_abort_other_conditions(
        self,
    ) -> None:
        time = np.linspace(0.0, 40.0, 401)
        concentration = xr.DataArray(
            np.zeros((1, 2, time.size), dtype=np.float64),
            dims=("channel", "chromo", "time"),
            coords={
                "channel": ["S1D1"],
                "chromo": ["HbO", "HbR"],
                "time": time,
                "samples": ("time", np.arange(time.size, dtype=np.int64)),
            },
        ).pint.quantify("micromolar")
        concentration.time.attrs["units"] = "s"
        stimulus = pd.DataFrame(
            [
                {"onset": 4.0, "duration": 1.0, "trial_type": "A"},
                {"onset": 10.0, "duration": 1.0, "trial_type": "B"},
            ]
        )

        summary, average, standard_error = _build_task_analysis(
            stimulus,
            concentration,
            [{"label": "S1D1", "passed": True}],
            set(),
            None,
            AnalysisConfig(),
        )

        self.assertTrue(summary["available"])
        self.assertIsNotNone(average)
        self.assertIsNotNone(standard_error)
        self.assertEqual([item["value"] for item in summary["conditions"]], ["B"])
        counts = {item["value"]: item for item in summary["condition_counts"]}
        self.assertEqual(counts["A"]["usable_count"], 0)
        self.assertEqual(counts["B"]["usable_count"], 1)
        self.assertEqual(summary["epoch_selection"]["excluded_boundary_events"], 1)


class InferenceReadinessGuardrailTests(unittest.TestCase):
    def test_reports_unavailable_when_glm_is_unavailable(self) -> None:
        readiness = _inference_readiness(
            [{"value": "A", "label": "条件 A", "usable_count": 3}],
            task_available=True,
            glm_available=False,
        )

        self.assertEqual(readiness["state"], "unavailable")
        self.assertEqual(readiness["interpretation_level"], "unavailable")
        self.assertFalse(readiness["inference_ready"])

    def test_reports_exploratory_when_any_condition_has_one_trial(self) -> None:
        readiness = _inference_readiness(
            [
                {"value": "A", "label": "条件 A", "usable_count": 1},
                {"value": "B", "label": "条件 B", "usable_count": 3},
            ],
            task_available=True,
            glm_available=True,
        )

        self.assertEqual(readiness["state"], "exploratory")
        self.assertEqual(readiness["interpretation_level"], "exploratory")
        self.assertFalse(readiness["inference_ready"])
        self.assertEqual(readiness["insufficient_conditions"], ["条件 A"])
        self.assertEqual(readiness["insufficient_condition_values"], ["A"])

    def test_reports_ready_when_every_condition_has_repeated_trials(self) -> None:
        readiness = _inference_readiness(
            [
                {"value": "A", "label": "条件 A", "usable_count": 2},
                {"value": "B", "label": "条件 B", "usable_count": 4},
            ],
            task_available=True,
            glm_available=True,
        )

        self.assertEqual(readiness["state"], "ready")
        self.assertEqual(readiness["interpretation_level"], "inferential")
        self.assertTrue(readiness["inference_ready"])
        self.assertEqual(readiness["insufficient_conditions"], [])


if __name__ == "__main__":
    unittest.main()
