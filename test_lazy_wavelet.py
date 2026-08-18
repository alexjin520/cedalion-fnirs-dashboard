"""Regression tests for the on-demand experimental Wavelet signal."""

import unittest
from unittest.mock import patch

import numpy as np
import xarray as xr

import server
from server import AnalysisConfig, AnalysisData, load_analysis, signal_payload


class _PintResult:
    """Small stand-in for Cedalion's pint-aware result in the focused test."""

    def __init__(self, value: xr.DataArray) -> None:
        self.pint = self
        self.value = value

    def to(self, _unit: str) -> xr.DataArray:
        return self.value


def _lazy_analysis() -> AnalysisData:
    time = np.arange(8, dtype=np.float64)
    od = xr.DataArray(
        np.ones((1, 2, time.size), dtype=np.float64),
        dims=("channel", "wavelength", "time"),
        coords={"channel": ["S1D1"], "wavelength": [760.0, 850.0], "time": time},
    )
    return AnalysisData(
        summary={},
        config=AnalysisConfig(),
        channels=[{"index": 0, "label": "S1D1", "source": "S1", "detector": "D1"}],
        series_options=[
            {
                "kind": "conc_wavelet_filtered",
                "label": "Wavelet",
                "components": [{"value": "HbO", "label": "HbO"}],
            }
        ],
        event_counts=[],
        events=[],
        intervals=[],
        quality=[],
        quality_summary={},
        motion_summary={},
        motion_segments=[],
        motion_clean_mask=None,
        series={"od": od},
        task_summary={},
        task_average=None,
        task_sem=None,
        glm_summary={},
        glm_condition_effects=[],
        glm_contrast_effects=[],
        wavelet_geometry=xr.DataArray([1.0]),
        wavelet_dpf=xr.DataArray([6.0]),
    )


class LazyWaveletTests(unittest.TestCase):
    def test_signal_request_computes_wavelet_once(self) -> None:
        analysis = _lazy_analysis()
        time = np.arange(8, dtype=np.float64)
        result = xr.DataArray(
            np.ones((1, 1, time.size), dtype=np.float64),
            dims=("channel", "chromo", "time"),
            coords={"channel": ["S1D1"], "chromo": ["HbO"], "time": time},
        )

        with (
            patch("server.motion.wavelet", return_value=analysis.series["od"]) as wavelet,
            patch("server.cw.od2conc", return_value=_PintResult(result)) as od2conc,
            patch("server.freq_filter", side_effect=lambda value, *_args: value) as filt,
        ):
            first = signal_payload(analysis, "conc_wavelet_filtered", 0, "HbO", 500)
            second = signal_payload(analysis, "conc_wavelet_filtered", 0, "HbO", 500)

        self.assertEqual(wavelet.call_count, 1)
        self.assertEqual(od2conc.call_count, 1)
        self.assertEqual(filt.call_count, 1)
        self.assertEqual(first["points"], second["points"])
        self.assertEqual(first["series"]["kind"], "conc_wavelet_filtered")

    def test_loading_real_sample_does_not_compute_wavelet(self) -> None:
        path = server.DEFAULT_DATA_DIR / "samples" / "mne_nirsport2_raw.snirf"
        if not path.is_file():
            self.skipTest("仓库中没有 mne_nirsport2_raw.snirf")

        server._load_analysis_cached.cache_clear()
        with patch("server.motion.wavelet", side_effect=AssertionError("Wavelet 应按需计算")) as wavelet:
            analysis = load_analysis(path, config=AnalysisConfig(), recording_id=path.name)

        self.assertIsNone(analysis.wavelet_filtered)
        wavelet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
