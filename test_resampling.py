"""Focused regression tests for the fNIRS amplitude resampling contract."""

import unittest

import numpy as np
import pandas as pd
import xarray as xr

from server import AnalysisConfig, analysis_config_from_payload


def amplitude_series(time: np.ndarray, values: np.ndarray | None = None) -> xr.DataArray:
    """Create a small valid CW-amplitude array with a supplied time axis."""
    time = np.asarray(time, dtype=np.float64)
    if values is None:
        values = 1.0 + 0.1 * np.sin(2 * np.pi * 0.25 * time)
    values = np.asarray(values, dtype=np.float64)
    data = np.stack((values, values * 1.02), axis=0)[np.newaxis, :, :]
    amplitudes = xr.DataArray(
        data,
        dims=("channel", "wavelength", "time"),
        coords={
            "channel": ["S1D1"],
            "source": ("channel", ["S1"]),
            "detector": ("channel", ["D1"]),
            "wavelength": [760.0, 850.0],
            "time": time,
            "samples": ("time", np.arange(time.size, dtype=np.int64)),
        },
        name="amplitudes",
    ).pint.quantify("V")
    amplitudes.time.attrs["units"] = "s"
    return amplitudes


def legacy_settings_payload() -> dict[str, object]:
    """A pre-resampling settings document emitted by the existing settings UI."""
    return {
        "dpf": 6.0,
        "filter_min_hz": 0.01,
        "filter_max_hz": 0.5,
        "snr_threshold": 2.0,
        "sci_threshold": 0.7,
        "psp_threshold": 0.1,
        "psp_min_clean_fraction": 0.75,
        "epoch_before_seconds": 5.0,
        "epoch_after_seconds": 20.0,
        "response_start_seconds": 3.0,
        "response_end_seconds": 15.0,
        "short_separation_mm": 15.0,
        "short_separation_mode": "report",
        "gvtd_mode": "report",
        "cbsi_mode": "off",
        "glm": {
            "noise_model": "ols",
            "drift_cutoff_hz": 0.003,
            "hrf_sigma_seconds": 3.0,
            "short_separation_mode": "auto",
            "ar_order": 30,
            "nuisance_mode": "off",
        },
    }


class ResamplingConfigurationTests(unittest.TestCase):
    def test_accepts_lowpass_only_filter_and_minimum_dpf(self) -> None:
        config = AnalysisConfig(dpf=0.01, filter_min_hz=0.0, filter_max_hz=0.5)
        config.validate()

        payload = legacy_settings_payload()
        payload["dpf"] = 0.01
        payload["filter_min_hz"] = 0.0
        result = analysis_config_from_payload(payload, AnalysisConfig())

        self.assertEqual(result.dpf, 0.01)
        self.assertEqual(result.filter_min_hz, 0.0)

    def test_legacy_payload_keeps_base_resampling_defaults(self) -> None:
        base = AnalysisConfig(
            resampling_mode="force",
            resampling_target_rate_hz=7.5,
            resampling_max_gap_seconds=0.35,
        )

        result = analysis_config_from_payload(legacy_settings_payload(), base)

        self.assertEqual(result.resampling_mode, "force")
        self.assertEqual(result.resampling_target_rate_hz, 7.5)
        self.assertEqual(result.resampling_max_gap_seconds, 0.35)

    def test_resampling_config_rejects_invalid_choices_and_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "FNIRS_RESAMPLING_MODE"):
            AnalysisConfig(resampling_mode="unexpected").validate()
        with self.assertRaisesRegex(ValueError, "FNIRS_RESAMPLING_TARGET_RATE_HZ"):
            AnalysisConfig(resampling_target_rate_hz=-1.0).validate()
        with self.assertRaisesRegex(ValueError, "FNIRS_RESAMPLING_MAX_GAP_SECONDS"):
            AnalysisConfig(resampling_max_gap_seconds=0.0).validate()


class TimeUnitNormalisationTests(unittest.TestCase):
    def test_millisecond_signal_and_events_are_copied_to_seconds(self) -> None:
        from server import _stimulus_in_seconds, _time_coordinate_in_seconds

        amplitudes = amplitude_series(np.array([0.0, 100.0, 200.0]))
        amplitudes.time.attrs["units"] = "ms"
        events = pd.DataFrame(
            [{"onset": 150.0, "duration": 25.0, "trial_type": "task"}]
        )

        normalised, time_audit = _time_coordinate_in_seconds(amplitudes)
        analysis_events, event_audit = _stimulus_in_seconds(
            events,
            time_audit["seconds_per_source_time_unit"],
        )

        np.testing.assert_allclose(normalised.time.values, [0.0, 0.1, 0.2])
        self.assertEqual(amplitudes.time.values.tolist(), [0.0, 100.0, 200.0])
        self.assertTrue(time_audit["converted"])
        self.assertEqual(analysis_events["onset"].tolist(), [0.15])
        self.assertEqual(analysis_events["duration"].tolist(), [0.025])
        self.assertEqual(events["onset"].tolist(), [150.0])
        self.assertEqual(events["duration"].tolist(), [25.0])
        self.assertTrue(event_audit["converted"])


class AmplitudeResamplingTests(unittest.TestCase):
    def setUp(self) -> None:
        from server import _resample_amplitudes

        self.resample = _resample_amplitudes

    def test_auto_regularises_irregular_time_and_keeps_integer_samples(self) -> None:
        source_time = np.array([10.0, 10.1, 10.22, 10.31, 10.4, 10.5, 10.6])
        events = pd.DataFrame(
            [{"onset": 10.2, "duration": 1.25, "trial_type": "task"}]
        )

        result, audit = self.resample(
            amplitudes=amplitude_series(source_time),
            stimulus=events,
            config=AnalysisConfig(
                resampling_mode="auto",
                resampling_target_rate_hz=10.0,
                resampling_max_gap_seconds=0.25,
            ),
        )

        output_time = np.asarray(result.time.values, dtype=np.float64)
        self.assertGreater(output_time.size, 2)
        np.testing.assert_allclose(np.diff(output_time), 0.1, atol=1e-10)
        np.testing.assert_array_equal(
            np.asarray(result.samples.values), np.arange(output_time.size)
        )
        self.assertTrue(np.issubdtype(result.samples.dtype, np.integer))
        self.assertTrue(audit["applied"])
        self.assertEqual(audit["mode"], "auto")
        self.assertAlmostEqual(audit["target_sample_rate_hz"], 10.0)

    def test_rejects_a_gap_larger_than_the_interpolation_limit(self) -> None:
        source_time = np.array([0.0, 0.1, 0.2, 2.0, 2.1, 2.2])

        with self.assertRaisesRegex(ValueError, "最大.*缺口|缺口.*最大"):
            self.resample(
                amplitudes=amplitude_series(source_time),
                stimulus=pd.DataFrame(),
                config=AnalysisConfig(
                    resampling_mode="auto",
                    resampling_target_rate_hz=10.0,
                    resampling_max_gap_seconds=0.5,
                ),
            )

    def test_force_downsample_records_anti_aliasing(self) -> None:
        source_time = np.arange(0.0, 8.0 + 1e-12, 0.05)
        signal = 1.0 + 0.08 * np.sin(2 * np.pi * 6.0 * source_time)

        result, audit = self.resample(
            amplitudes=amplitude_series(source_time, signal),
            stimulus=pd.DataFrame(),
            config=AnalysisConfig(
                resampling_mode="force",
                resampling_target_rate_hz=5.0,
                resampling_max_gap_seconds=0.2,
            ),
        )

        np.testing.assert_allclose(
            np.diff(np.asarray(result.time.values, dtype=np.float64)), 0.2, atol=1e-10
        )
        self.assertTrue(audit["anti_aliasing"]["applied"])
        self.assertEqual(audit["anti_aliasing"]["order"], 8)
        self.assertIsNotNone(audit["anti_aliasing"]["cutoff_hz"])
        self.assertAlmostEqual(audit["target_sample_rate_hz"], 5.0)

    def test_resampling_never_mutates_event_onsets_or_durations(self) -> None:
        events = pd.DataFrame(
            [
                {"onset": 1.2, "duration": 0.0, "trial_type": "instant"},
                {"onset": 4.5, "duration": 2.25, "trial_type": "block"},
            ]
        )
        expected = events[["onset", "duration"]].copy(deep=True)

        _, audit = self.resample(
            amplitudes=amplitude_series(
                np.array([0.0, 0.1, 0.21, 0.3, 0.4, 0.5, 0.6])
            ),
            stimulus=events,
            config=AnalysisConfig(
                resampling_mode="auto",
                resampling_target_rate_hz=10.0,
                resampling_max_gap_seconds=0.25,
            ),
        )

        pd.testing.assert_frame_equal(events[["onset", "duration"]], expected)
        self.assertEqual(
            audit["event_timing"]["policy"], "preserve_onset_duration_seconds"
        )


class AmplitudeValueGapTests(unittest.TestCase):
    def test_excludes_a_channel_with_a_long_invalid_intensity_gap(self) -> None:
        from server import _prepare_amplitudes

        time = np.arange(0.0, 1.1, 0.1)
        values = np.ones((2, 2, time.size), dtype=np.float64)
        values[0, :, 3:8] = np.nan
        amplitudes = xr.DataArray(
            values,
            dims=("channel", "wavelength", "time"),
            coords={
                "channel": ["S1D1", "S2D1"],
                "source": ("channel", ["S1", "S2"]),
                "detector": ("channel", ["D1", "D1"]),
                "wavelength": [760.0, 850.0],
                "time": time,
                "samples": ("time", np.arange(time.size, dtype=np.int64)),
            },
        ).pint.quantify("V")
        amplitudes.time.attrs["units"] = "s"

        prepared, audit = _prepare_amplitudes(
            amplitudes,
            AnalysisConfig(
                min_positive_fraction=0.5,
                resampling_max_gap_seconds=0.25,
            ),
        )

        self.assertEqual(prepared.channel.values.tolist(), ["S2D1"])
        self.assertEqual(audit["excluded_value_interpolation_gap_channels"], 1)
        self.assertIn("S1D1", audit["excluded_nonpositive_channel_labels"])
        self.assertGreater(audit["maximum_value_interpolation_gap_seconds"], 0.25)


if __name__ == "__main__":
    unittest.main()
