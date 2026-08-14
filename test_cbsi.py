import unittest
from unittest.mock import patch

import numpy as np
import xarray as xr

from server import (
    AnalysisConfig,
    _cbsi_correct,
    _censor_glm_timepoints,
    analysis_config_from_payload,
    _quality_rows,
)


def concentration(values: np.ndarray) -> xr.DataArray:
    return xr.DataArray(
        values,
        dims=("chromo", "channel", "time"),
        coords={
            "chromo": ["HbO", "HbR"],
            "channel": [f"S{index + 1}D1" for index in range(values.shape[1])],
            "time": np.arange(values.shape[2], dtype=np.float64),
        },
        name="concentration",
    ).pint.quantify("micromolar")


class CbsiCorrectionTests(unittest.TestCase):
    def test_applies_cui_formula_and_forces_negative_correlation(self) -> None:
        source = concentration(
            np.array(
                [
                    [[1.0, 2.0, 3.0, 4.0]],
                    [[2.0, 4.0, 1.0, 3.0]],
                ]
            )
        )
        source.time.attrs["units"] = "s"

        corrected, status = _cbsi_correct(source)

        expected_hbo = np.array([-0.5, -1.0, 1.0, 0.5])
        np.testing.assert_allclose(
            corrected.sel(chromo="HbO").pint.dequantify().values[0],
            expected_hbo,
        )
        np.testing.assert_allclose(
            corrected.sel(chromo="HbR").pint.dequantify().values[0],
            -expected_hbo,
        )
        self.assertEqual(str(corrected.pint.units), "micromolar")
        self.assertEqual(corrected.time.attrs["units"], "s")
        self.assertEqual(status["corrected_channels"], 1)
        self.assertEqual(status["skipped_channels"], [])

    def test_preserves_channel_when_hbr_has_zero_variance(self) -> None:
        source = concentration(
            np.array(
                [
                    [[1.0, 2.0, 3.0, 4.0]],
                    [[2.0, 2.0, 2.0, 2.0]],
                ]
            )
        )

        corrected, status = _cbsi_correct(source)

        np.testing.assert_allclose(
            corrected.pint.dequantify().values,
            source.pint.dequantify().values,
        )
        self.assertEqual(status["corrected_channels"], 0)
        self.assertEqual(status["skipped_channels"], ["S1D1"])

    def test_requires_both_chromophores(self) -> None:
        source = concentration(np.ones((2, 1, 4))).sel(chromo=["HbO"])
        with self.assertRaisesRegex(ValueError, "HbO 和 HbR"):
            _cbsi_correct(source)


class CbsiConfigurationTests(unittest.TestCase):
    def test_old_saved_settings_default_to_environment_cbsi_mode(self) -> None:
        payload = {
            "dpf": 6.0,
            "filter_min_hz": 0.01,
            "filter_max_hz": 0.5,
            "snr_threshold": 2.0,
            "sci_threshold": 0.7,
            "epoch_before_seconds": 5.0,
            "epoch_after_seconds": 20.0,
            "response_start_seconds": 3.0,
            "response_end_seconds": 15.0,
            "short_separation_mm": 15.0,
            "short_separation_mode": "report",
            "gvtd_mode": "report",
            "glm": {
                "noise_model": "ols",
                "drift_cutoff_hz": 0.003,
                "hrf_sigma_seconds": 3.0,
                "short_separation_mode": "auto",
                "ar_order": 30,
                "nuisance_mode": "off",
            },
        }

        config = analysis_config_from_payload(
            payload,
            AnalysisConfig(cbsi_mode="on"),
        )

        self.assertEqual(config.cbsi_mode, "on")


class GlmGvtdCensoringTests(unittest.TestCase):
    def test_censors_target_and_common_and_channelwise_design_rows_together(self) -> None:
        import cedalion.models.glm as glm

        target = xr.DataArray(
            np.arange(12.0).reshape(2, 6),
            dims=("channel", "time"),
            coords={"channel": ["S1D1", "S2D1"], "time": np.arange(6.0)},
        )
        common = xr.DataArray(
            np.arange(18.0).reshape(6, 3),
            dims=("time", "regressor"),
            coords={"time": np.arange(6.0), "regressor": ["a", "b", "c"]},
        )
        channel_wise = xr.DataArray(
            np.arange(12.0).reshape(2, 6),
            dims=("channel", "time"),
            coords={"channel": ["S1D1", "S2D1"], "time": np.arange(6.0)},
        ).expand_dims(regressor=["short"])
        design = glm.design_matrix.DesignMatrix(
            common=common,
            channel_wise=[channel_wise],
        )

        censored, censored_design, status = _censor_glm_timepoints(
            target,
            design,
            np.array([True, False, True, False, True, True]),
        )

        np.testing.assert_array_equal(censored.time.values, [0.0, 2.0, 4.0, 5.0])
        np.testing.assert_array_equal(
            censored_design.common.time.values, [0.0, 2.0, 4.0, 5.0]
        )
        np.testing.assert_array_equal(
            censored_design.channel_wise[0].time.values, [0.0, 2.0, 4.0, 5.0]
        )
        self.assertEqual(status["excluded_samples"], 2)
        self.assertEqual(status["retained_samples"], 4)

    def test_rejects_mismatched_gvtd_mask(self) -> None:
        import cedalion.models.glm as glm

        target = xr.DataArray(
            np.ones((1, 3)),
            dims=("channel", "time"),
            coords={"channel": ["S1D1"], "time": np.arange(3.0)},
        )
        with self.assertRaisesRegex(ValueError, "掩码与 GLM 时间轴长度不一致"):
            _censor_glm_timepoints(
                target,
                glm.design_matrix.DesignMatrix(),
                np.ones(2, dtype=bool),
            )


class PspQualityGateTests(unittest.TestCase):
    def test_psp_clean_fraction_is_required_for_automatic_pass(self) -> None:
        config = AnalysisConfig(
            snr_threshold=2.0,
            sci_threshold=0.7,
            psp_computation_threshold=0.1,
            psp_min_clean_fraction=0.75,
        )
        channels = [{"index": 0, "label": "S1D1", "source": "S1", "detector": "D1"}]

        snr = xr.DataArray([[4.0, 3.0]], dims=("channel", "time"), coords={"channel": ["S1D1"]})
        sci = xr.DataArray([[0.9, 0.9]], dims=("channel", "time"), coords={"channel": ["S1D1"]})
        psp = xr.DataArray([[0.08, 0.12, 0.12, 0.12]], dims=("channel", "time"), coords={"channel": ["S1D1"]})
        psp_mask = psp > 0.1
        with patch("server.quality.snr", return_value=(snr, None)), patch(
            "server.quality.sci", return_value=(sci, None)
        ), patch("server.quality.psp", return_value=(psp, psp_mask)):
            rows, summary = _quality_rows(concentration(np.ones((2, 1, 4))), channels, config, set())
        self.assertTrue(rows[0]["passed"])
        self.assertAlmostEqual(rows[0]["psp_clean_fraction"], 0.75)
        self.assertTrue(summary["psp_is_quality_gate"])

        psp_mask = xr.DataArray([[True, False, False, False]], dims=("channel", "time"), coords={"channel": ["S1D1"]})
        with patch("server.quality.snr", return_value=(snr, None)), patch(
            "server.quality.sci", return_value=(sci, None)
        ), patch("server.quality.psp", return_value=(psp, psp_mask)):
            rows, _ = _quality_rows(concentration(np.ones((2, 1, 4))), channels, config, set())
        self.assertFalse(rows[0]["passed"])
        self.assertTrue(any("PSP 合格窗口比例" in reason for reason in rows[0]["exclusion_reasons"]))


if __name__ == "__main__":
    unittest.main()
