"""Regression tests for malformed amplitude and stimulus inputs."""

import unittest

import numpy as np
import pandas as pd
import xarray as xr

from server import AnalysisConfig, _validate_recording_input


def valid_amplitudes(time: np.ndarray) -> xr.DataArray:
    data = np.ones((1, 2, time.size), dtype=np.float64)
    amplitudes = xr.DataArray(
        data,
        dims=("channel", "wavelength", "time"),
        coords={
            "channel": ["S1D1"],
            "source": ("channel", ["S1"]),
            "detector": ("channel", ["D1"]),
            "wavelength": [760.0, 850.0],
            "time": time,
        },
    ).pint.quantify("V")
    amplitudes.time.attrs["units"] = "s"
    return amplitudes


class InputFailureRegressionTests(unittest.TestCase):
    def test_rejects_nonfinite_time_coordinate(self) -> None:
        with self.assertRaisesRegex(ValueError, "时间坐标包含非有限值"):
            _validate_recording_input(
                valid_amplitudes(np.array([0.0, 0.1, np.nan])),
                pd.DataFrame(),
                AnalysisConfig(),
            )

    def test_rejects_non_increasing_time_coordinate(self) -> None:
        with self.assertRaisesRegex(ValueError, "时间坐标必须严格递增"):
            _validate_recording_input(
                valid_amplitudes(np.array([0.0, 0.1, 0.1])),
                pd.DataFrame(),
                AnalysisConfig(),
            )

    def test_rejects_missing_required_amplitude_coordinates(self) -> None:
        amplitudes = valid_amplitudes(np.array([0.0, 0.1, 0.2])).drop_vars("detector")
        with self.assertRaisesRegex(ValueError, "缺少坐标.*detector"):
            _validate_recording_input(amplitudes, pd.DataFrame(), AnalysisConfig())

    def test_reports_missing_stimulus_columns_without_corrupting_analysis(self) -> None:
        result = _validate_recording_input(
            valid_amplitudes(np.array([0.0, 0.1, 0.2])),
            pd.DataFrame({"value": [1], "onset": [0.1]}),
            AnalysisConfig(),
        )
        self.assertTrue(result["valid"])
        self.assertTrue(
            any("刺激事件缺少 trial_type 列" in item for item in result["warnings"])
        )
        self.assertTrue(
            any("刺激事件缺少 duration 列" in item for item in result["warnings"])
        )

    def test_warns_for_nonfinite_stimulus_onset(self) -> None:
        result = _validate_recording_input(
            valid_amplitudes(np.array([0.0, 0.1, 0.2])),
            pd.DataFrame(
                [{"onset": np.nan, "duration": 0.0, "trial_type": "A"}]
            ),
            AnalysisConfig(),
        )
        self.assertIn("刺激 onset 列包含非有限值", result["warnings"])


if __name__ == "__main__":
    unittest.main()
