"""Focused tests for the shared, on-demand GLM analysis state."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from unittest.mock import patch

import server
from server import AnalysisConfig, AnalysisData


def _analysis() -> AnalysisData:
    config = AnalysisConfig()
    quality = [
        {
            "index": 0,
            "label": "S1D1",
            "source": "S1",
            "detector": "D1",
            "passed": True,
            "task_channel_eligible": True,
        }
    ]
    task = {
        "available": True,
        "conditions": [{"value": "A", "label": "条件 A", "count": 2}],
        "condition_counts": [
            {"value": "A", "label": "条件 A", "total_count": 2, "usable_count": 2}
        ],
        "short_separation": {"excluded_channel_labels": []},
    }
    summary = {
        "analysis": {"id": "analysis-test", "input_sha256": "a" * 64},
        "nuisance_regression": {
            "short_separation": {},
            "auxiliary": {},
            "global": {},
        },
        "short_separation": {"short_channel_count": 0},
        "input_validation": {"auxiliary": {"count": 0}},
        "manual_quality_control": {},
    }
    placeholder = server._pending_glm_summary(task, config, 0, 0)
    return AnalysisData(
        summary=summary,
        config=config,
        channels=quality,
        series_options=[],
        event_counts=[],
        events=[],
        intervals=[],
        quality=quality,
        quality_summary={},
        motion_summary={},
        motion_segments=[],
        motion_clean_mask=None,
        series={},
        task_summary=task,
        task_average=None,
        task_sem=None,
        glm_summary=placeholder,
        glm_condition_effects=[],
        glm_contrast_effects=[],
        glm_context={
            "stim": object(),
            "concentration": object(),
            "quality_rows": quality,
            "short_channel_labels": set(),
            "geo3d": object(),
            "auxiliary_timeseries": {},
            "recording_time_unit": "second",
            "config": config,
            "motion_clean_mask": None,
            "auxiliary_signals": {"count": 0},
            "cbsi_requested": False,
            "cbsi_applied": False,
        },
    )


def _glm_result(available: bool = True) -> tuple[dict, list, list]:
    summary = {
        "available": available,
        "conditions": [{"value": "A", "label": "条件 A", "count": 2}],
        "contrasts": [],
        "channel_labels": ["S1D1"] if available else [],
        "channels": 1 if available else 0,
        "model": {"input": "test", "noise_model": "ols"},
        "short_separation": {"applied": False, "reason": "none"},
        "auxiliary": {"applied": False, "reason": "none"},
        "global": {"applied": False, "reason": "none"},
        "gvtd": {"applied": False},
        "regressors": [],
    }
    return summary, [], []


class LazyGlmTests(unittest.TestCase):
    def test_include_glm_false_does_not_fit(self) -> None:
        analysis = _analysis()
        with (
            patch("server._load_analysis_cached", return_value=analysis),
            patch("server._build_glm_analysis") as builder,
        ):
            result = server.load_analysis(
                Path(__file__),
                config=analysis.config,
                recording_id="test.snirf",
                include_glm=False,
            )
        self.assertIs(result, analysis)
        self.assertEqual(result.glm_state, "pending")
        builder.assert_not_called()

    def test_repeated_and_concurrent_ensure_fit_once(self) -> None:
        analysis = _analysis()
        with patch("server._build_glm_analysis", return_value=_glm_result()) as builder:
            with ThreadPoolExecutor(max_workers=4) as pool:
                states = list(pool.map(lambda _index: server._ensure_glm_analysis(analysis), range(8)))
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(analysis.glm_state, "ready")
        self.assertTrue(analysis.glm_summary["available"])
        self.assertTrue(all(item is analysis for item in states))
        server._ensure_glm_analysis(analysis)
        self.assertEqual(builder.call_count, 1)

    def test_failed_fit_is_cached_as_terminal_state(self) -> None:
        analysis = _analysis()
        with patch("server._build_glm_analysis", side_effect=RuntimeError("boom")) as builder:
            server._ensure_glm_analysis(analysis)
            server._ensure_glm_analysis(analysis)
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(analysis.glm_state, "failed")
        self.assertEqual(analysis.glm_summary["status"], "failed")
        self.assertIn("boom", analysis.glm_summary["error"])

    def test_auto_channel_selects_the_first_modelled_channel(self) -> None:
        analysis = _analysis()
        analysis.glm_summary = _glm_result()[0]
        selected = server._glm_channel(analysis, -1)
        self.assertEqual(selected["label"], "S1D1")


if __name__ == "__main__":
    unittest.main()
