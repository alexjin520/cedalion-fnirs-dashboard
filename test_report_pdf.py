import re
import unittest
from unittest.mock import patch

from matplotlib import ft2font
import numpy as np
import xarray as xr

import server
from server import (
    AnalysisConfig,
    AnalysisData,
    _report_font,
    report_pdf_bytes,
    task_csv_bytes,
    task_glm_csv_bytes,
)


class PdfReportTests(unittest.TestCase):
    @staticmethod
    def exploratory_analysis() -> AnalysisData:
        reltime = np.linspace(-5.0, 20.0, 51)
        values = np.zeros((1, 1, 2, reltime.size), dtype=np.float64)
        values[0, 0, 0] = np.exp(-((reltime - 5.0) ** 2) / 12.0)
        values[0, 0, 1] = -0.5 * values[0, 0, 0]
        average = xr.DataArray(
            values,
            dims=("trial_type", "channel", "chromo", "reltime"),
            coords={
                "trial_type": ["A"],
                "channel": ["S1D1"],
                "chromo": ["HbO", "HbR"],
                "reltime": reltime,
            },
        )
        standard_error = xr.full_like(average, np.nan)
        readiness = {
            "state": "exploratory",
            "ready": False,
            "inference_ready": False,
            "minimum_trials_per_condition": 2,
            "condition_counts": [
                {"value": "A", "label": "条件 A", "total_count": 1, "usable_count": 1}
            ],
            "insufficient_conditions": ["条件 A"],
            "reason": "至少一个条件少于 2 个可用重复试次，GLM 数值仅供探索性查看",
        }
        summary = {
            "filename": "exploratory.snirf",
            "analysis": {
                "id": "analysis-exploratory",
                "input_sha256": "a" * 64,
                "created_at_utc": "2026-08-14T00:00:00Z",
            },
            "subject": {"display_name": "subject-001"},
            "cedalion_version": "test",
            "sample_rate_hz": 10.0,
            "duration_seconds": 120.0,
            "wavelengths_nm": [760.0, 850.0],
            "dpf": 6.0,
            "raw_channels": 1,
            "analyzed_channels": 1,
            "manual_quality_control": {"bad_channel_labels": []},
            "recording": {"id": "exploratory.snirf", "filename": "exploratory.snirf"},
        }
        task_summary = {
            "available": True,
            "conditions": [
                {
                    "value": "A",
                    "label": "条件 A",
                    "count": 1,
                    "total_count": 1,
                    "usable_count": 1,
                    "duration_seconds": 2.0,
                }
            ],
            "condition_counts": readiness["condition_counts"],
            "motion_correction": "TDDR",
            "stimulus_duration_seconds": 2.0,
            "short_separation": {},
            "inference_readiness": readiness,
        }
        glm_summary = {
            "available": True,
            "conditions": task_summary["conditions"],
            "contrasts": [],
            "channel_labels": ["S1D1"],
            "model": {"noise_model": "ols"},
            "inference_readiness": readiness,
        }
        quality = [
            {"index": 0, "label": "S1D1", "source": "S1", "detector": "D1", "passed": True}
        ]
        return AnalysisData(
            summary=summary,
            config=AnalysisConfig(),
            channels=[quality[0]],
            series_options=[],
            event_counts=[],
            events=[],
            intervals=[],
            quality=quality,
            quality_summary={
                "passed_channels": 1,
                "total_channels": 1,
                "snr_threshold": 2.0,
                "sci_threshold": 0.7,
                "psp_min_clean_fraction": 0.75,
            },
            motion_summary={"flagged_samples": 0, "total_samples": 1200},
            motion_segments=[],
            motion_clean_mask=None,
            series={},
            task_summary=task_summary,
            task_average=average,
            task_sem=standard_error,
            glm_summary=glm_summary,
            glm_condition_effects=[
                {
                    "channel": quality[0],
                    "chromo": "HbO",
                    "condition": "A",
                    "condition_label": "条件 A",
                    "beta": 0.2,
                    "confidence_interval_95": [0.1, 0.3],
                    "t_value": 2.0,
                    "p_value": 0.08,
                    "q_value": 0.12,
                    "degrees_of_freedom": 20.0,
                    "r_squared": 0.2,
                }
            ],
            glm_contrast_effects=[],
        )

    def test_report_font_covers_chinese_latin_numbers_and_scientific_units(self) -> None:
        font = _report_font()
        face = ft2font.FT2Font(font.get_file())

        for character in "分析报告 Cedalion fNIRS 0123456789 µM — ≥":
            self.assertNotEqual(face.get_char_index(ord(character)), 0, character)

    def test_report_has_three_pages_when_task_response_is_unavailable(self) -> None:
        analysis = AnalysisData(
            summary={
                "filename": "recording.snirf",
                "analysis": {
                    "id": "analysis-test",
                    "input_sha256": "a" * 64,
                    "created_at_utc": "2026-08-14T00:00:00Z",
                },
                "subject": {"display_name": "subject-001"},
                "cedalion_version": "test",
                "sample_rate_hz": 10.0,
                "duration_seconds": 120.0,
                "wavelengths_nm": [760.0, 850.0],
                "dpf": 6.0,
                "raw_channels": 2,
                "analyzed_channels": 2,
                "manual_quality_control": {"bad_channel_labels": []},
            },
            config=AnalysisConfig(),
            channels=[],
            series_options=[],
            event_counts=[],
            events=[],
            intervals=[],
            quality=[],
            quality_summary={
                "passed_channels": 1,
                "total_channels": 2,
                "snr_threshold": 2.0,
                "sci_threshold": 0.7,
                "psp_min_clean_fraction": 0.75,
            },
            motion_summary={"flagged_samples": 2, "total_samples": 100},
            motion_segments=[],
            motion_clean_mask=None,
            series={},
            task_summary={"conditions": [], "motion_correction": "TDDR"},
            task_average=None,
            task_sem=None,
            glm_summary={"available": False, "model": {}, "channel_labels": []},
            glm_condition_effects=[],
            glm_contrast_effects=[],
        )

        content, filename = report_pdf_bytes(analysis)

        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(len(re.findall(rb"/Type /Page\b", content)), 3)
        self.assertEqual(filename, "fnirs-report-recording.snirf-analysis-test.pdf")

    def test_exploratory_exports_and_pdf_keep_readiness_state(self) -> None:
        analysis = self.exploratory_analysis()

        task_body, _ = task_csv_bytes(analysis, "A", 0)
        task_header, task_row = task_body.decode("utf-8-sig").splitlines()[:2]
        self.assertIn("inference_state", task_header)
        self.assertIn("repeat_trial_ready", task_header)
        self.assertIn(",exploratory,False,", task_row)

        glm_body, _ = task_glm_csv_bytes(analysis)
        glm_header, glm_row = glm_body.decode("utf-8-sig").splitlines()[:2]
        self.assertIn("effect_usable_trials_minimum", glm_header)
        self.assertIn("inference_reason", glm_header)
        self.assertIn(",exploratory,False,", glm_row)

        with patch("server._report_card", wraps=server._report_card) as report_card:
            content, _ = report_pdf_bytes(analysis)
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(len(re.findall(rb"/Type /Page\b", content)), 3)
        self.assertTrue(
            any(
                call.args[5] == "GLM 状态" and call.args[6] == "仅探索性"
                for call in report_card.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
