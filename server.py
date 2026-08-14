#!/usr/bin/env python3
"""Server-side Cedalion CW-fNIRS analysis dashboard."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
from importlib import metadata as importlib_metadata
import json
import math
import os
import platform
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("MPLBACKEND", "Agg")

import cedalion
import cedalion.io
import cedalion.models.glm as glm
import cedalion.nirs.cw as cw
import h5py
import numpy as np
import xarray as xr
from scipy.signal import butter, sosfiltfilt
from cedalion.nirs import channel_distances, split_long_short_channels
from cedalion.sigproc import motion, quality
from cedalion.sigproc.frequency import freq_filter


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DATA_DIR = BASE_DIR / "data" / "samples"
DEFAULT_FILENAME = "recording_20260419_173115.snirf"
DASHBOARD_VERSION = "2026.08.13"
ANALYSIS_MANIFEST_VERSION = "1.2"
ANALYSIS_PROTOCOL_VERSION = "snirf-od-hb-cbsi-psp-qc-task-glm-gvtd-censor-v6"
RECORDING_QUERY_PARAMETER = "recording"
SNIRF_SUFFIX = ".snirf"
HASH_CHUNK_BYTES = 1024 * 1024
SNIRF_SUBJECT_ID_SUFFIX = "metaDataTags/SubjectID"
REPRODUCIBILITY_PACKAGES = (
    "cedalion",
    "click",
    "h5py",
    "joblib",
    "matplotlib",
    "mne",
    "nibabel",
    "numpy",
    "pandas",
    "pint-xarray",
    "pooch",
    "pybtex",
    "pyvista",
    "PyWavelets",
    "scikit-image",
    "scikit-learn",
    "scipy",
    "snirf",
    "statsmodels",
    "StrEnum",
    "trimesh",
    "vtk",
    "xarray",
)

OFFICIAL_TAPPING_LABELS = {
    "1.0": "Control",
    "2.0": "Tapping/Left",
    "3.0": "Tapping/Right",
    "15.0": "Marker",
}
CONDITION_LABELS = {
    "Control": "对照",
    "Tapping/Left": "左手敲击",
    "Tapping/Right": "右手敲击",
}
SHORT_SEPARATION_MODES = ("report", "exclude")
GLM_NOISE_MODELS = ("ols", "ar_irls")
GLM_SHORT_SEPARATION_MODES = ("off", "auto")
GLM_NUISANCE_MODES = ("off", "auxiliary", "global", "auxiliary_global")
GVTD_MODES = ("report", "exclude_epochs")
CBSI_MODES = ("off", "on")
ANALYSIS_LOAD_LOCK = RLock()
ANALYSIS_CONFIG_LOCK = RLock()


def _environment_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字，当前值为 {raw_value!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限数字")
    return value


def _environment_float_with_alias(
    name: str,
    alias: str,
    default: float,
) -> float:
    primary_value = os.getenv(name)
    if primary_value is not None and primary_value.strip():
        return _environment_float(name, default)
    return _environment_float(alias, default)


def _environment_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    value = raw_value.strip().lower()
    if value not in choices:
        allowed = "、".join(choices)
        raise ValueError(f"{name} 必须是 {allowed} 之一，当前值为 {raw_value!r}")
    return value


def _environment_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数，当前值为 {raw_value!r}") from exc
    return value


def _environment_csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Read a comma-separated, ordered list without guessing signal names."""
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    values = tuple(value.strip() for value in raw_value.split(","))
    if not all(values) or len(set(values)) != len(values):
        raise ValueError(f"{name} 必须是无重复且非空的逗号分隔名称列表")
    return values


@dataclass(frozen=True)
class AnalysisConfig:
    """Scientific settings read once when the server starts."""

    dpf: float = 6.0
    filter_min_hz: float = 0.01
    filter_max_hz: float = 0.5
    snr_threshold: float = 2.0
    sci_threshold: float = 0.7
    sci_window_seconds: float = 5.0
    psp_window_seconds: float = 5.0
    # Cedalion's PSP mask threshold; the old environment variable remains an alias.
    psp_computation_threshold: float = 0.1
    psp_min_clean_fraction: float = 0.75
    epoch_before_seconds: float = 5.0
    epoch_after_seconds: float = 20.0
    response_start_seconds: float = 3.0
    response_end_seconds: float = 15.0
    min_positive_fraction: float = 0.99
    geometry_min_distance_mm: float = 1.0
    geometry_max_distance_mm: float = 60.0
    short_separation_mm: float = 15.0
    short_separation_mode: str = "report"
    glm_noise_model: str = "ols"
    glm_drift_cutoff_hz: float = 0.003
    glm_hrf_sigma_seconds: float = 3.0
    glm_short_separation_mode: str = "auto"
    glm_ar_order: int = 30
    glm_nuisance_mode: str = "off"
    glm_auxiliary_signal_names: tuple[str, ...] = ()
    glm_auxiliary_max_gap_seconds: float = 1.0
    gvtd_mode: str = "report"
    cbsi_mode: str = "off"

    @classmethod
    def from_environment(cls) -> "AnalysisConfig":
        config = cls(
            dpf=_environment_float("FNIRS_DPF", cls.dpf),
            filter_min_hz=_environment_float("FNIRS_FILTER_MIN_HZ", cls.filter_min_hz),
            filter_max_hz=_environment_float("FNIRS_FILTER_MAX_HZ", cls.filter_max_hz),
            snr_threshold=_environment_float("FNIRS_SNR_THRESHOLD", cls.snr_threshold),
            sci_threshold=_environment_float("FNIRS_SCI_THRESHOLD", cls.sci_threshold),
            sci_window_seconds=_environment_float("FNIRS_SCI_WINDOW_SECONDS", cls.sci_window_seconds),
            psp_window_seconds=_environment_float("FNIRS_PSP_WINDOW_SECONDS", cls.psp_window_seconds),
            psp_computation_threshold=_environment_float_with_alias(
                "FNIRS_PSP_THRESHOLD",
                "FNIRS_PSP_COMPUTATION_THRESHOLD",
                cls.psp_computation_threshold,
            ),
            psp_min_clean_fraction=_environment_float(
                "FNIRS_PSP_MIN_CLEAN_FRACTION",
                cls.psp_min_clean_fraction,
            ),
            epoch_before_seconds=_environment_float(
                "FNIRS_EPOCH_BEFORE_SECONDS",
                cls.epoch_before_seconds,
            ),
            epoch_after_seconds=_environment_float(
                "FNIRS_EPOCH_AFTER_SECONDS",
                cls.epoch_after_seconds,
            ),
            response_start_seconds=_environment_float(
                "FNIRS_RESPONSE_START_SECONDS",
                cls.response_start_seconds,
            ),
            response_end_seconds=_environment_float(
                "FNIRS_RESPONSE_END_SECONDS",
                cls.response_end_seconds,
            ),
            min_positive_fraction=_environment_float(
                "FNIRS_MIN_POSITIVE_FRACTION",
                cls.min_positive_fraction,
            ),
            geometry_min_distance_mm=_environment_float(
                "FNIRS_GEOMETRY_MIN_DISTANCE_MM",
                cls.geometry_min_distance_mm,
            ),
            geometry_max_distance_mm=_environment_float(
                "FNIRS_GEOMETRY_MAX_DISTANCE_MM",
                cls.geometry_max_distance_mm,
            ),
            short_separation_mm=_environment_float_with_alias(
                "FNIRS_SHORT_SEPARATION_THRESHOLD_MM",
                "FNIRS_SHORT_SEPARATION_MM",
                cls.short_separation_mm,
            ),
            short_separation_mode=_environment_choice(
                "FNIRS_SHORT_SEPARATION_MODE",
                cls.short_separation_mode,
                SHORT_SEPARATION_MODES,
            ),
            glm_noise_model=_environment_choice(
                "FNIRS_GLM_NOISE_MODEL",
                cls.glm_noise_model,
                GLM_NOISE_MODELS,
            ),
            glm_drift_cutoff_hz=_environment_float(
                "FNIRS_GLM_DRIFT_CUTOFF_HZ",
                cls.glm_drift_cutoff_hz,
            ),
            glm_hrf_sigma_seconds=_environment_float(
                "FNIRS_GLM_HRF_SIGMA_SECONDS",
                cls.glm_hrf_sigma_seconds,
            ),
            glm_short_separation_mode=_environment_choice(
                "FNIRS_GLM_SHORT_SEPARATION_MODE",
                cls.glm_short_separation_mode,
                GLM_SHORT_SEPARATION_MODES,
            ),
            glm_ar_order=_environment_int("FNIRS_GLM_AR_ORDER", cls.glm_ar_order),
            glm_nuisance_mode=_environment_choice(
                "FNIRS_GLM_NUISANCE_MODE",
                cls.glm_nuisance_mode,
                GLM_NUISANCE_MODES,
            ),
            glm_auxiliary_signal_names=_environment_csv(
                "FNIRS_GLM_AUXILIARY_SIGNALS",
                cls.glm_auxiliary_signal_names,
            ),
            glm_auxiliary_max_gap_seconds=_environment_float(
                "FNIRS_GLM_AUXILIARY_MAX_GAP_SECONDS",
                cls.glm_auxiliary_max_gap_seconds,
            ),
            gvtd_mode=_environment_choice(
                "FNIRS_GVTD_MODE", cls.gvtd_mode, GVTD_MODES
            ),
            cbsi_mode=_environment_choice(
                "FNIRS_CBSI_MODE", cls.cbsi_mode, CBSI_MODES
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        errors: list[str] = []
        if self.dpf <= 0:
            errors.append("FNIRS_DPF 必须大于 0")
        if self.filter_min_hz < 0 or self.filter_max_hz <= self.filter_min_hz:
            errors.append("滤波范围必须满足 0 <= 下限 < 上限")
        if self.snr_threshold < 0:
            errors.append("FNIRS_SNR_THRESHOLD 不能小于 0")
        if not 0 <= self.sci_threshold <= 1:
            errors.append("FNIRS_SCI_THRESHOLD 必须在 0 到 1 之间")
        if self.sci_window_seconds <= 0 or self.psp_window_seconds <= 0:
            errors.append("SCI 和 PSP 窗口必须大于 0")
        if self.psp_computation_threshold < 0:
            errors.append("FNIRS_PSP_THRESHOLD 不能小于 0")
        if not 0 <= self.psp_min_clean_fraction <= 1:
            errors.append("FNIRS_PSP_MIN_CLEAN_FRACTION 必须在 0 到 1 之间")
        if self.epoch_before_seconds < 0 or self.epoch_after_seconds <= 0:
            errors.append("任务分段窗口无效")
        if (
            self.response_start_seconds < 0
            or self.response_end_seconds <= self.response_start_seconds
            or self.response_end_seconds > self.epoch_after_seconds
        ):
            errors.append("响应窗口必须位于刺激后的任务分段内")
        if not 0 < self.min_positive_fraction <= 1:
            errors.append("FNIRS_MIN_POSITIVE_FRACTION 必须在 0 到 1 之间")
        if (
            self.geometry_min_distance_mm < 0
            or self.geometry_max_distance_mm <= self.geometry_min_distance_mm
        ):
            errors.append("探头距离范围无效")
        if self.short_separation_mm <= 0:
            errors.append("FNIRS_SHORT_SEPARATION_THRESHOLD_MM 必须大于 0")
        if self.short_separation_mode not in SHORT_SEPARATION_MODES:
            errors.append(
                "FNIRS_SHORT_SEPARATION_MODE 必须是 "
                + "、".join(SHORT_SEPARATION_MODES)
                + " 之一"
            )
        if self.glm_noise_model not in GLM_NOISE_MODELS:
            errors.append(
                "FNIRS_GLM_NOISE_MODEL 必须是 "
                + "、".join(GLM_NOISE_MODELS)
                + " 之一"
            )
        if self.glm_drift_cutoff_hz <= 0:
            errors.append("FNIRS_GLM_DRIFT_CUTOFF_HZ 必须大于 0")
        if self.glm_hrf_sigma_seconds <= 0:
            errors.append("FNIRS_GLM_HRF_SIGMA_SECONDS 必须大于 0")
        if self.glm_short_separation_mode not in GLM_SHORT_SEPARATION_MODES:
            errors.append(
                "FNIRS_GLM_SHORT_SEPARATION_MODE 必须是 "
                + "、".join(GLM_SHORT_SEPARATION_MODES)
                + " 之一"
            )
        if self.glm_ar_order <= 0:
            errors.append("FNIRS_GLM_AR_ORDER 必须大于 0")
        if self.glm_nuisance_mode not in GLM_NUISANCE_MODES:
            errors.append(
                "FNIRS_GLM_NUISANCE_MODE 必须是 "
                + "、".join(GLM_NUISANCE_MODES)
                + " 之一"
            )
        if self.glm_auxiliary_max_gap_seconds <= 0:
            errors.append("FNIRS_GLM_AUXILIARY_MAX_GAP_SECONDS 必须大于 0")
        if self.gvtd_mode not in GVTD_MODES:
            errors.append("FNIRS_GVTD_MODE 必须是 " + "、".join(GVTD_MODES) + " 之一")
        if self.cbsi_mode not in CBSI_MODES:
            errors.append("FNIRS_CBSI_MODE 必须是 " + "、".join(CBSI_MODES) + " 之一")
        if errors:
            raise ValueError("；".join(errors))

    def metadata(self) -> dict[str, Any]:
        return {
            "dpf": self.dpf,
            "filter_hz": [self.filter_min_hz, self.filter_max_hz],
            "snr_threshold": self.snr_threshold,
            "sci_threshold": self.sci_threshold,
            "sci_window_seconds": self.sci_window_seconds,
            "psp_window_seconds": self.psp_window_seconds,
            "psp_threshold": self.psp_computation_threshold,
            "psp_min_clean_fraction": self.psp_min_clean_fraction,
            "epoch_seconds": [-self.epoch_before_seconds, self.epoch_after_seconds],
            "response_window_seconds": [
                self.response_start_seconds,
                self.response_end_seconds,
            ],
            "minimum_positive_fraction": self.min_positive_fraction,
            "geometry_distance_limits_mm": [
                self.geometry_min_distance_mm,
                self.geometry_max_distance_mm,
            ],
            "short_separation": {
                "threshold_mm": self.short_separation_mm,
                "mode": self.short_separation_mode,
            },
            "glm": {
                "noise_model": self.glm_noise_model,
                "drift_cutoff_hz": self.glm_drift_cutoff_hz,
                "hrf_sigma_seconds": self.glm_hrf_sigma_seconds,
                "short_separation_mode": self.glm_short_separation_mode,
                "ar_order": self.glm_ar_order,
                "nuisance_mode": self.glm_nuisance_mode,
                "auxiliary_signal_names": list(self.glm_auxiliary_signal_names),
                "auxiliary_resampling": {
                    "method": "linear",
                    "max_gap_seconds": self.glm_auxiliary_max_gap_seconds,
                    "standardization": "zscore_after_resampling",
                },
            },
            "gvtd": {
                "mode": self.gvtd_mode,
                "scope": (
                    "task_average_epochs_and_glm_samples"
                    if self.gvtd_mode == "exclude_epochs"
                    else "report_only"
                ),
            },
            "cbsi": {
                "mode": self.cbsi_mode,
                "scope": (
                    "task_average_and_glm"
                    if self.cbsi_mode == "on"
                    else "continuous_comparison_only"
                ),
                "method": "Cui et al. correlation-based signal improvement",
            },
        }


def _json_number(payload: dict[str, Any], name: str) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限数字")
    return value


def _json_integer(payload: dict[str, Any], name: str) -> int:
    value = _json_number(payload, name)
    if not value.is_integer():
        raise ValueError(f"{name} 必须是整数")
    return int(value)


def _json_choice(
    payload: dict[str, Any], name: str, choices: tuple[str, ...]
) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} 必须是 " + "、".join(choices) + " 之一")
    return value


def analysis_config_from_payload(
    payload: dict[str, Any],
    base: AnalysisConfig,
) -> AnalysisConfig:
    """Build a validated UI configuration without accepting arbitrary fields."""
    glm = payload.get("glm")
    if not isinstance(glm, dict):
        raise ValueError("glm 必须是对象")
    config = replace(
        base,
        dpf=_json_number(payload, "dpf"),
        filter_min_hz=_json_number(payload, "filter_min_hz"),
        filter_max_hz=_json_number(payload, "filter_max_hz"),
        snr_threshold=_json_number(payload, "snr_threshold"),
        sci_threshold=_json_number(payload, "sci_threshold"),
        psp_computation_threshold=(
            _json_number(payload, "psp_threshold")
            if "psp_threshold" in payload
            else base.psp_computation_threshold
        ),
        psp_min_clean_fraction=(
            _json_number(payload, "psp_min_clean_fraction")
            if "psp_min_clean_fraction" in payload
            else base.psp_min_clean_fraction
        ),
        epoch_before_seconds=_json_number(payload, "epoch_before_seconds"),
        epoch_after_seconds=_json_number(payload, "epoch_after_seconds"),
        response_start_seconds=_json_number(payload, "response_start_seconds"),
        response_end_seconds=_json_number(payload, "response_end_seconds"),
        short_separation_mm=_json_number(payload, "short_separation_mm"),
        short_separation_mode=_json_choice(
            payload, "short_separation_mode", SHORT_SEPARATION_MODES
        ),
        gvtd_mode=_json_choice(payload, "gvtd_mode", GVTD_MODES),
        cbsi_mode=(
            _json_choice(payload, "cbsi_mode", CBSI_MODES)
            if "cbsi_mode" in payload
            else base.cbsi_mode
        ),
        glm_noise_model=_json_choice(glm, "noise_model", GLM_NOISE_MODELS),
        glm_drift_cutoff_hz=_json_number(glm, "drift_cutoff_hz"),
        glm_hrf_sigma_seconds=_json_number(glm, "hrf_sigma_seconds"),
        glm_short_separation_mode=_json_choice(
            glm, "short_separation_mode", GLM_SHORT_SEPARATION_MODES
        ),
        glm_ar_order=_json_integer(glm, "ar_order"),
        glm_nuisance_mode=_json_choice(glm, "nuisance_mode", GLM_NUISANCE_MODES),
    )
    config.validate()
    return config


def analysis_config_payload(config: AnalysisConfig, source: str) -> dict[str, Any]:
    return {
        "ok": True,
        "source": source,
        "persistence": (
            "已保存到服务器，重启后继续使用"
            if source == "网页设置"
            else "使用服务器启动时的环境变量配置"
        ),
        "settings": {
            "dpf": config.dpf,
            "filter_min_hz": config.filter_min_hz,
            "filter_max_hz": config.filter_max_hz,
            "snr_threshold": config.snr_threshold,
            "sci_threshold": config.sci_threshold,
            "psp_threshold": config.psp_computation_threshold,
            "psp_min_clean_fraction": config.psp_min_clean_fraction,
            "epoch_before_seconds": config.epoch_before_seconds,
            "epoch_after_seconds": config.epoch_after_seconds,
            "response_start_seconds": config.response_start_seconds,
            "response_end_seconds": config.response_end_seconds,
            "short_separation_mm": config.short_separation_mm,
            "short_separation_mode": config.short_separation_mode,
            "gvtd_mode": config.gvtd_mode,
            "cbsi_mode": config.cbsi_mode,
            "glm": {
                "noise_model": config.glm_noise_model,
                "drift_cutoff_hz": config.glm_drift_cutoff_hz,
                "hrf_sigma_seconds": config.glm_hrf_sigma_seconds,
                "short_separation_mode": config.glm_short_separation_mode,
                "ar_order": config.glm_ar_order,
                "nuisance_mode": config.glm_nuisance_mode,
            },
        },
    }


def _utc_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _package_version(name: str) -> str | None:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return None


def _software_versions() -> dict[str, Any]:
    return {
        "dashboard_version": DASHBOARD_VERSION,
        "analysis_protocol_version": ANALYSIS_PROTOCOL_VERSION,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": platform.platform(),
        "packages": {
            name: _package_version(name) for name in REPRODUCIBILITY_PACKAGES
        },
    }


def _recording_id(path: Path, data_dir: Path) -> str:
    try:
        return path.resolve().relative_to(data_dir.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("记录文件必须位于 FNIRS_DATA_DIR 内") from exc


def _recording_entities(recording_id: str) -> dict[str, str | None]:
    def entity_value(name: str) -> str | None:
        match = re.search(rf"(?:^|[/_]){name}-([^/_]+)", recording_id)
        return match.group(1) if match else None

    return {
        "subject": entity_value("sub"),
        "session": entity_value("ses"),
    }


def _recording_descriptor(
    path: Path,
    data_dir: Path,
    default_path: Path,
) -> dict[str, Any]:
    stat = path.stat()
    recording_id = _recording_id(path, data_dir)
    entities = _recording_entities(recording_id)
    return {
        "id": recording_id,
        "filename": path.name,
        "size_bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "modified_at_utc": _utc_timestamp(stat.st_mtime),
        "subject": entities["subject"],
        "session": entities["session"],
        "is_default": path.resolve() == default_path.resolve(),
    }


def list_recordings(
    data_dir: Path,
    default_path: Path,
    subject: str | None = None,
    session: str | None = None,
) -> list[dict[str, Any]]:
    """List only resolvable SNIRF files contained by the configured data root."""
    if not data_dir.is_dir():
        return []
    recordings: list[dict[str, Any]] = []
    for candidate in data_dir.rglob("*"):
        if candidate.suffix.lower() != SNIRF_SUFFIX or not candidate.is_file():
            continue
        try:
            descriptor = _recording_descriptor(candidate.resolve(), data_dir, default_path)
        except (OSError, ValueError):
            continue
        if subject and descriptor["subject"] != subject:
            continue
        if session and descriptor["session"] != session:
            continue
        recordings.append(descriptor)
    return sorted(
        recordings,
        key=lambda item: (not item["is_default"], item["id"].casefold()),
    )


def resolve_recording(
    data_dir: Path,
    default_path: Path,
    requested_id: str | None,
) -> tuple[Path, str]:
    """Resolve a query selection without allowing data-directory escapes."""
    if requested_id is None:
        return default_path, _recording_id(default_path, data_dir)
    if not requested_id or "\x00" in requested_id:
        raise ValueError("recording 参数不能为空")
    requested_path = Path(requested_id)
    if requested_path.is_absolute():
        raise ValueError("recording 必须是 FNIRS_DATA_DIR 内的相对路径")
    path = (data_dir / requested_path).resolve()
    recording_id = _recording_id(path, data_dir)
    if path.suffix.lower() != SNIRF_SUFFIX:
        raise ValueError("recording 必须指向 .snirf 文件")
    if not path.is_file():
        raise FileNotFoundError(f"找不到记录文件：{recording_id}")
    return path, recording_id


@lru_cache(maxsize=8)
def _file_sha256(filename: str, size_bytes: int, modified_ns: int) -> str:
    """Hash an unchanged input once for its manifest and derived exports."""
    del size_bytes, modified_ns
    digest = hashlib.sha256()
    with Path(filename).open("rb") as source:
        while chunk := source.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _analysis_identity(
    input_sha256: str,
    config: AnalysisConfig,
    software: dict[str, Any],
    manual_qc: dict[str, Any],
) -> tuple[str, str]:
    identity = {
        "input_sha256": input_sha256,
        "protocol_version": ANALYSIS_PROTOCOL_VERSION,
        "parameters": config.metadata(),
        "software": software,
        "manual_qc": manual_qc,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    return f"analysis-{fingerprint[:20]}", fingerprint


def _read_hdf5_utf8_string(dataset: h5py.Dataset) -> str:
    """Decode a scalar or length-one HDF5 string dataset as UTF-8."""
    value = dataset[()]
    if isinstance(value, np.ndarray):
        if value.size != 1:
            raise ValueError(f"{dataset.name} 必须是单个字符串")
        value = value.reshape(-1)[0]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{dataset.name} 不是有效的 UTF-8 字符串") from exc
    raise ValueError(f"{dataset.name} 不是字符串数据集")


def _snirf_subject_id_datasets(path: Path) -> list[tuple[str, str]]:
    """Read SubjectID directly because pysnirf2 accepts ASCII metadata only."""
    subject_ids: list[tuple[str, str]] = []
    with h5py.File(path, "r") as source:
        def collect(name: str, value: object) -> None:
            if not name.endswith(SNIRF_SUBJECT_ID_SUFFIX):
                return
            if not isinstance(value, h5py.Dataset):
                raise ValueError(f"/{name} 必须是 HDF5 数据集")
            subject_ids.append((name, _read_hdf5_utf8_string(value)))

        source.visititems(collect)
    return subject_ids


def _subject_metadata(subject_ids: list[tuple[str, str]]) -> dict[str, Any]:
    values = list(dict.fromkeys(value for _, value in subject_ids))
    return {
        "display_name": " / ".join(values) if values else None,
        "metadata_paths": [f"/{name}" for name, _ in subject_ids],
    }


def _replace_subject_ids_with_ascii(
    path: Path,
    subject_ids: list[tuple[str, str]],
    anonymous_identifier: str,
) -> None:
    """Replace only non-ASCII SubjectID datasets in an analysis-only copy."""
    string_dtype = h5py.string_dtype(encoding="ascii")
    with h5py.File(path, "r+") as analysis_copy:
        for dataset_path, value in subject_ids:
            if value.isascii():
                continue
            dataset = analysis_copy[dataset_path]
            attributes = dict(dataset.attrs.items())
            dataset_name = dataset.name.rsplit("/", 1)[-1]
            parent = dataset.parent
            replacement = (
                np.full(dataset.shape, anonymous_identifier, dtype=object)
                if dataset.shape
                else anonymous_identifier
            )
            del parent[dataset_name]
            rewritten = parent.create_dataset(
                dataset_name,
                data=replacement,
                dtype=string_dtype,
            )
            for key, attribute in attributes.items():
                rewritten.attrs[key] = attribute


def _read_snirf_with_subject_id_compatibility(
    path: Path,
    input_sha256: str,
) -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
    """Read UTF-8 SubjectID for display without modifying the source SNIRF."""
    subject_ids = _snirf_subject_id_datasets(path)
    non_ascii_paths = [name for name, value in subject_ids if not value.isascii()]
    compatibility = {
        "source_file_modified": False,
        "temporary_analysis_copy_used": bool(non_ascii_paths),
        "rewritten_metadata_paths": [f"/{name}" for name in non_ascii_paths],
    }
    if not non_ascii_paths:
        return cedalion.io.read_snirf(path), _subject_metadata(subject_ids), compatibility

    # pysnirf2 0.8 decodes metadata as ASCII; only this disposable copy is rewritten.
    anonymous_identifier = f"subject-{input_sha256[:12]}"
    with tempfile.TemporaryDirectory(prefix="fnirs-snirf-") as directory:
        analysis_copy = Path(directory) / "analysis.snirf"
        shutil.copyfile(path, analysis_copy)
        _replace_subject_ids_with_ascii(analysis_copy, subject_ids, anonymous_identifier)
        recordings = cedalion.io.read_snirf(analysis_copy)
    return recordings, _subject_metadata(subject_ids), compatibility


class QcDecisionStore:
    """Small, atomic file store for user-entered bad-channel decisions."""

    def __init__(self, filename: Path) -> None:
        self.filename = filename
        self.lock = RLock()

    def _read(self) -> dict[str, Any]:
        if not self.filename.is_file():
            return {"version": 1, "recordings": {}}
        try:
            payload = json.loads(self.filename.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取人工质量决定文件：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("recordings", {}), dict):
            raise ValueError("人工质量决定文件格式无效")
        return payload

    def for_recording(self, recording_id: str) -> dict[str, Any]:
        with self.lock:
            payload = self._read()
            record = payload["recordings"].get(recording_id, {})
            labels = record.get("bad_channel_labels", []) if isinstance(record, dict) else []
            return {
                "bad_channel_labels": sorted({str(label) for label in labels}),
                "updated_at_utc": record.get("updated_at_utc") if isinstance(record, dict) else None,
            }

    def update(self, recording_id: str, labels: list[str]) -> dict[str, Any]:
        cleaned = sorted({str(label).strip() for label in labels if str(label).strip()})
        with self.lock:
            payload = self._read()
            payload.setdefault("version", 1)
            recordings = payload.setdefault("recordings", {})
            if cleaned:
                recordings[recording_id] = {
                    "bad_channel_labels": cleaned,
                    "updated_at_utc": _utc_now(),
                }
            else:
                recordings.pop(recording_id, None)
            self.filename.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.filename.with_suffix(self.filename.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.filename)
            return self.for_recording(recording_id)


class AnalysisConfigStore:
    """Atomic persistent store for the single dashboard analysis configuration."""

    def __init__(self, filename: Path, environment_config: AnalysisConfig) -> None:
        self.filename = filename
        self.environment_config = environment_config
        self.lock = RLock()

    def load(self) -> tuple[AnalysisConfig, str]:
        with self.lock:
            if not self.filename.is_file():
                return self.environment_config, "服务器环境变量"
            try:
                payload = json.loads(self.filename.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"无法读取分析设置文件：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("分析设置文件格式无效")
            return analysis_config_from_payload(payload, self.environment_config), "网页设置"

    def save(self, payload: dict[str, Any]) -> AnalysisConfig:
        config = analysis_config_from_payload(payload, self.environment_config)
        settings = analysis_config_payload(config, "网页设置")["settings"]
        with self.lock:
            self.filename.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.filename.with_suffix(self.filename.suffix + ".tmp")
            temporary.write_text(
                json.dumps(settings, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.filename)
        return config

    def reset(self) -> AnalysisConfig:
        with self.lock:
            if self.filename.is_file():
                self.filename.unlink()
        return self.environment_config


def _finite_number(value: float) -> float | None:
    return float(value) if math.isfinite(float(value)) else None


def _values(data_array: xr.DataArray) -> np.ndarray:
    try:
        data_array = data_array.pint.dequantify()
    except Exception:
        pass
    return np.asarray(data_array.values, dtype=np.float64)


def _cbsi_correct(
    concentration: xr.DataArray,
) -> tuple[xr.DataArray, dict[str, Any]]:
    """Apply correlation-based signal improvement independently per channel.

    CBSI assumes the neuronal HbO and HbR responses are perfectly negatively
    correlated. The standard-deviation ratio is estimated over the full time
    series, following Cui et al. (2010), NeuroImage 49(4), 3039-3046.
    """
    required_dimensions = {"chromo", "channel", "time"}
    if not required_dimensions.issubset(concentration.dims):
        missing = "、".join(sorted(required_dimensions - set(concentration.dims)))
        raise ValueError(f"CBSI 输入缺少维度：{missing}")
    chromophores = {str(value) for value in concentration.chromo.values}
    if not {"HbO", "HbR"}.issubset(chromophores):
        raise ValueError("CBSI 输入必须同时包含 HbO 和 HbR")

    hbo = concentration.sel(chromo="HbO")
    hbr = concentration.sel(chromo="HbR")
    alpha = hbo.std("time") / hbr.std("time")
    alpha_values = _values(alpha)
    valid_values = np.isfinite(alpha_values) & (alpha_values > np.finfo(float).eps)
    valid = xr.DataArray(
        valid_values,
        dims=alpha.dims,
        coords=alpha.coords,
    )
    corrected_hbo = xr.where(valid, (hbo - alpha * hbr) / 2, hbo)
    corrected_hbr = xr.where(valid, -(hbo - alpha * hbr) / (2 * alpha), hbr)
    corrected = xr.concat(
        [corrected_hbo, corrected_hbr],
        dim=xr.DataArray(["HbO", "HbR"], dims="chromo", name="chromo"),
    ).transpose(*concentration.dims)
    corrected.name = concentration.name
    corrected.attrs = concentration.attrs.copy()
    for coordinate in concentration.coords:
        if coordinate in corrected.coords:
            corrected.coords[coordinate].attrs = (
                concentration.coords[coordinate].attrs.copy()
            )

    channel_labels = [str(value) for value in concentration.channel.values]
    valid_flat = np.asarray(valid_values, dtype=bool).reshape(-1)
    finite_alpha = alpha_values[valid_values]
    skipped = [
        channel_labels[index]
        for index, is_valid in enumerate(valid_flat)
        if not is_valid
    ]
    status = {
        "method": "Cui et al. correlation-based signal improvement",
        "formula": {
            "alpha": "std(HbO) / std(HbR)",
            "hbo": "(HbO - alpha * HbR) / 2",
            "hbr": "-(HbO - alpha * HbR) / (2 * alpha)",
        },
        "estimated_over": "full_time_series_per_channel",
        "channels": len(channel_labels),
        "corrected_channels": int(np.count_nonzero(valid_flat)),
        "skipped_channels": skipped,
        "alpha": {
            "minimum": _finite_number(np.min(finite_alpha)) if finite_alpha.size else None,
            "median": _finite_number(np.median(finite_alpha)) if finite_alpha.size else None,
            "maximum": _finite_number(np.max(finite_alpha)) if finite_alpha.size else None,
        },
    }
    return corrected, status


def _display_unit(data_array: xr.DataArray, kind: str) -> str:
    try:
        unit = str(data_array.pint.units)
    except Exception:
        unit = ""
    if kind == "od":
        return "ΔOD"
    if unit in {"micromolar", "µM"}:
        return "µM"
    if unit in {"volt", "V"}:
        return "V"
    if unit in {"dimensionless", "None", ""}:
        return "a.u."
    return unit


def _safe_mean(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return _finite_number(np.mean(finite)) if finite.size else None


def _safe_min(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return _finite_number(np.min(finite)) if finite.size else None


def _unit_text(data_array: xr.DataArray) -> str:
    try:
        return str(data_array.pint.units)
    except Exception:
        return "unknown"


def _raise_validation_errors(errors: list[str]) -> None:
    if errors:
        raise ValueError("SNIRF 输入校验失败：" + "；".join(errors))


def _validate_recording_input(
    amplitudes: xr.DataArray,
    stimulus: Any,
    config: AnalysisConfig,
) -> dict[str, Any]:
    """Validate signal structure before any OD or concentration calculation."""
    errors: list[str] = []
    warnings: list[str] = []
    required_dims = {"channel", "wavelength", "time"}
    required_coords = {"channel", "source", "detector", "wavelength", "time"}
    missing_dims = sorted(required_dims.difference(amplitudes.dims))
    missing_coords = sorted(required_coords.difference(amplitudes.coords))
    if missing_dims:
        errors.append("缺少维度：" + ", ".join(missing_dims))
    if missing_coords:
        errors.append("缺少坐标：" + ", ".join(missing_coords))

    wavelengths: list[float] = []
    if "wavelength" in amplitudes.coords:
        try:
            wavelength_values = np.asarray(amplitudes.wavelength.values, dtype=np.float64)
        except (TypeError, ValueError):
            errors.append("波长坐标不是数值")
        else:
            if wavelength_values.size < 2:
                errors.append("计算 HbO/HbR 至少需要两个波长")
            elif not np.all(np.isfinite(wavelength_values)) or np.any(wavelength_values <= 0):
                errors.append("波长必须是有限正数")
            elif np.unique(wavelength_values).size != wavelength_values.size:
                errors.append("波长不能重复")
            else:
                wavelengths = [float(value) for value in wavelength_values]
                if np.any((wavelength_values < 650) | (wavelength_values > 1000)):
                    warnings.append("存在 650–1000 nm 常用范围外的波长，请核对消光系数")

    sample_rate: float | None = None
    sampling_interval: float | None = None
    is_uniform: bool | None = None
    if "time" in amplitudes.coords:
        try:
            time = np.asarray(amplitudes.time.values, dtype=np.float64)
        except (TypeError, ValueError):
            errors.append("时间坐标不是数值")
        else:
            if time.size < 2:
                errors.append("至少需要两个时间采样点")
            elif not np.all(np.isfinite(time)):
                errors.append("时间坐标包含非有限值")
            else:
                steps = np.diff(time)
                if np.any(steps <= 0):
                    errors.append("时间坐标必须严格递增")
                else:
                    sampling_interval = float(np.median(steps))
                    sample_rate = float(1.0 / sampling_interval)
                    is_uniform = bool(np.allclose(steps, sampling_interval, rtol=0.01, atol=1e-9))
                    if not is_uniform:
                        warnings.append("采样间隔不均匀，滤波结果需额外核对")
                    if config.filter_max_hz >= sample_rate / 2:
                        errors.append(
                            f"滤波上限 {config.filter_max_hz:g} Hz 不低于 Nyquist "
                            f"频率 {sample_rate / 2:g} Hz"
                        )

    unit = _unit_text(amplitudes)
    unit_normalisation = unit in {"dimensionless", "None", "", "unknown"}
    if unit_normalisation:
        warnings.append("原始光强未带物理单位，将按相对电压处理")

    stimulus_columns = list(getattr(stimulus, "columns", []))
    stimulus_count = len(stimulus) if stimulus is not None else 0
    if stimulus_count == 0:
        warnings.append("没有刺激事件，任务分析将不可用")
    elif "onset" not in stimulus_columns:
        warnings.append("刺激事件缺少 onset 列，任务分析将不可用")
    else:
        try:
            onsets = np.asarray(stimulus["onset"], dtype=np.float64)
        except (TypeError, ValueError):
            warnings.append("刺激 onset 列不是数值，任务分析将不可用")
        else:
            if not np.all(np.isfinite(onsets)):
                warnings.append("刺激 onset 列包含非有限值")
    if stimulus_count and "trial_type" not in stimulus_columns:
        warnings.append("刺激事件缺少 trial_type 列，无法按条件分析")
    if stimulus_count and "duration" not in stimulus_columns:
        warnings.append("刺激事件缺少 duration 列，将按瞬时事件处理")

    _raise_validation_errors(errors)
    return {
        "valid": True,
        "warnings": warnings,
        "amplitude_unit": unit,
        "amplitude_unit_normalised": unit_normalisation,
        "wavelengths_nm": wavelengths,
        "sample_rate_hz": sample_rate,
        "sampling_interval_seconds": sampling_interval,
        "sampling_is_uniform": is_uniform,
        "stimulus": {
            "events": stimulus_count,
            "columns": stimulus_columns,
        },
    }


def _normalise_amplitude_units(
    amplitudes: xr.DataArray,
    input_validation: dict[str, Any],
) -> tuple[xr.DataArray, dict[str, Any]]:
    if not input_validation["amplitude_unit_normalised"]:
        return amplitudes, {
            "source_unit": input_validation["amplitude_unit"],
            "analysis_unit": _unit_text(amplitudes),
            "normalised": False,
        }
    try:
        normalised = amplitudes.pint.dequantify().pint.quantify("V")
    except Exception as exc:
        raise ValueError(f"无法为原始光强设置相对电压单位：{exc}") from exc
    return normalised, {
        "source_unit": input_validation["amplitude_unit"],
        "analysis_unit": _unit_text(normalised),
        "normalised": True,
    }


def _validate_geometry(
    amplitudes: xr.DataArray,
    geo3d: xr.DataArray,
    config: AnalysisConfig,
) -> dict[str, Any]:
    """Verify usable source-detector geometry and summarize channel distances."""
    errors: list[str] = []
    warnings: list[str] = []
    geometry_unit = _unit_text(geo3d)
    try:
        crs = geo3d.points.crs
    except Exception:
        crs = ""
        errors.append("探头几何缺少 Cedalion 坐标定义")
    if not crs or crs not in geo3d.dims or geo3d.sizes.get(crs) != 3:
        errors.append("探头几何必须包含三个空间坐标")
    if "label" not in geo3d.coords:
        errors.append("探头几何缺少 label 坐标")

    labels: set[str] = set()
    positions: np.ndarray | None = None
    if "label" in geo3d.coords:
        label_values = [str(value) for value in geo3d.label.values]
        labels = set(label_values)
        if len(labels) != len(label_values):
            errors.append("探头几何包含重复 label")
    try:
        positions = np.asarray(
            geo3d.pint.to("millimeter").pint.dequantify().values,
            dtype=np.float64,
        )
    except Exception:
        errors.append("探头几何缺少可转换为长度的单位")
    if positions is not None and not np.all(np.isfinite(positions)):
        errors.append("探头坐标包含非有限值")

    source_labels = {str(value) for value in amplitudes.source.values}
    detector_labels = {str(value) for value in amplitudes.detector.values}
    missing_labels = sorted((source_labels | detector_labels).difference(labels))
    if missing_labels:
        errors.append("探头几何缺少通道所需光极：" + ", ".join(missing_labels[:8]))

    _raise_validation_errors(errors)
    try:
        distances = channel_distances(amplitudes, geo3d).pint.to("millimeter")
        distance_values = _values(distances)
    except Exception as exc:
        raise ValueError(f"无法计算源-探测器距离：{exc}") from exc
    if not np.all(np.isfinite(distance_values)):
        raise ValueError("SNIRF 输入校验失败：源-探测器距离包含非有限值")
    if np.any(distance_values <= config.geometry_min_distance_mm):
        raise ValueError(
            "SNIRF 输入校验失败：存在不大于 "
            f"{config.geometry_min_distance_mm:g} mm 的源-探测器距离"
        )
    above_recommended = int(
        np.count_nonzero(distance_values > config.geometry_max_distance_mm)
    )
    if above_recommended:
        warnings.append(
            f"{above_recommended} 个通道的源-探测器距离超过 "
            f"{config.geometry_max_distance_mm:g} mm"
        )
    return {
        "valid": True,
        "warnings": warnings,
        "unit": geometry_unit,
        "optodes": len(labels),
        "sources": len(source_labels),
        "detectors": len(detector_labels),
        "validated_channels": int(distance_values.size),
        "distance_mm": {
            "minimum": _safe_min(distance_values),
            "median": _finite_number(np.median(distance_values)),
            "maximum": _finite_number(np.max(distance_values)),
            "above_recommended": above_recommended,
        },
    }


def _prepare_amplitudes(
    amplitudes: xr.DataArray,
    config: AnalysisConfig,
) -> tuple[xr.DataArray, dict[str, Any]]:
    """Remove device matrix entries that cannot be converted to optical density.

    Some acquisition files contain every possible source-detector pair and fill
    unused pairs with zero.  OD is logarithmic, so non-positive intensities are
    invalid.  Keep channels that are valid for at least 99% of samples at every
    wavelength, then linearly bridge their isolated invalid samples.
    """
    raw_channel_count = int(amplitudes.sizes.get("channel", 0))
    if raw_channel_count == 0:
        raise ValueError("SNIRF 中没有测量通道")
    raw_channel_labels = [str(channel) for channel in amplitudes.channel.values]

    try:
        dequantified = amplitudes.pint.dequantify()
    except Exception:
        dequantified = amplitudes

    valid = np.isfinite(dequantified) & (dequantified > 0)
    positive_fraction = valid.mean("time")
    if "wavelength" in positive_fraction.dims:
        positive_fraction = positive_fraction.min("wavelength")
    keep = np.asarray(positive_fraction.values, dtype=np.float64) >= config.min_positive_fraction
    keep_indices = np.flatnonzero(keep)
    excluded_nonpositive_indices = np.flatnonzero(~keep)
    if keep_indices.size == 0:
        raise ValueError(
            "没有通道在每个波长上达到 "
            f"{config.min_positive_fraction:.0%} 的正光强采样率"
        )

    selected = dequantified.isel(channel=keep_indices)
    selected_valid = np.isfinite(selected) & (selected > 0)
    interpolated_samples = int((~selected_valid).sum().item())
    if interpolated_samples:
        selected = selected.where(selected_valid).interpolate_na(
            "time",
            method="linear",
            fill_value="extrapolate",
        )
    remaining_invalid = int(
        (~(np.isfinite(selected) & (selected > 0))).sum().item()
    )
    if remaining_invalid:
        raise ValueError(f"有效通道插值后仍有 {remaining_invalid} 个无效光强采样")

    try:
        selected = selected.pint.quantify()
    except Exception:
        pass
    return selected, {
        "raw_channels": raw_channel_count,
        "analyzed_channels": int(keep_indices.size),
        "excluded_nonpositive_channels": raw_channel_count - int(keep_indices.size),
        "excluded_nonpositive_channel_labels": [
            raw_channel_labels[index] for index in excluded_nonpositive_indices
        ],
        "interpolated_samples": interpolated_samples,
        "minimum_positive_fraction": config.min_positive_fraction,
    }


@dataclass
class AnalysisData:
    summary: dict[str, Any]
    config: AnalysisConfig
    channels: list[dict[str, Any]]
    series_options: list[dict[str, Any]]
    event_counts: list[dict[str, Any]]
    events: list[dict[str, Any]]
    intervals: list[dict[str, Any]]
    quality: list[dict[str, Any]]
    quality_summary: dict[str, Any]
    motion_summary: dict[str, Any]
    motion_segments: list[dict[str, Any]]
    motion_clean_mask: np.ndarray | None
    series: dict[str, xr.DataArray]
    task_summary: dict[str, Any]
    task_average: xr.DataArray | None
    task_sem: xr.DataArray | None
    glm_summary: dict[str, Any]
    glm_condition_effects: list[dict[str, Any]]
    glm_contrast_effects: list[dict[str, Any]]


def _channel_text(data: xr.DataArray, coordinate: str, channel: Any, fallback: str) -> str:
    if coordinate not in data.coords:
        return fallback
    try:
        return str(data.coords[coordinate].sel(channel=channel).item())
    except Exception:
        return fallback


def _build_channels(amplitudes: xr.DataArray) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, channel in enumerate(amplitudes.channel.values):
        label = str(channel)
        result.append(
            {
                "index": index,
                "label": label,
                "source": _channel_text(amplitudes, "source", channel, "—"),
                "detector": _channel_text(amplitudes, "detector", channel, "—"),
            }
        )
    return result


def _short_separation_summary(
    amplitudes: xr.DataArray,
    geo3d: xr.DataArray,
    config: AnalysisConfig,
) -> dict[str, Any]:
    """Classify analyzed channels with Cedalion's distance-based splitter."""
    threshold = config.short_separation_mm * cedalion.units.mm
    try:
        long_channels, short_channels = split_long_short_channels(
            amplitudes,
            geo3d,
            distance_threshold=threshold,
        )
        distances = channel_distances(amplitudes, geo3d).pint.to("millimeter")
    except Exception as exc:
        raise ValueError(f"无法识别短距离通道：{exc}") from exc

    distance_values = _values(distances)
    distance_by_label = {
        str(channel): _finite_number(distance)
        for channel, distance in zip(distances.channel.values, distance_values)
    }
    short_labels = {str(channel) for channel in short_channels.channel.values}
    channel_details = [
        {
            "label": str(channel),
            "source": _channel_text(amplitudes, "source", channel, "—"),
            "detector": _channel_text(amplitudes, "detector", channel, "—"),
            "distance_mm": distance_by_label.get(str(channel)),
            "classification": "short" if str(channel) in short_labels else "long",
        }
        for channel in amplitudes.channel.values
    ]
    short_details = [
        channel for channel in channel_details if channel["classification"] == "short"
    ]
    return {
        "threshold_mm": config.short_separation_mm,
        "classification": "short: distance < threshold; long: distance >= threshold",
        "mode": config.short_separation_mode,
        "scope": "task_average_only",
        "analyzed_channels": int(amplitudes.sizes["channel"]),
        "short_channel_count": int(short_channels.sizes["channel"]),
        "long_channel_count": int(long_channels.sizes["channel"]),
        "channels": channel_details,
        "short_channels": short_details,
    }


def _auxiliary_signal_inventory(
    recording: Any,
    fnirs_time: np.ndarray,
) -> dict[str, Any]:
    """Describe SNIRF aux streams without using them as regressors."""
    fnirs_time = fnirs_time[np.isfinite(fnirs_time)]
    fnirs_start = _finite_number(np.min(fnirs_time)) if fnirs_time.size else None
    fnirs_end = _finite_number(np.max(fnirs_time)) if fnirs_time.size else None
    time_unit = str(recording.meta_data.get("TimeUnit", "unknown"))
    time_is_seconds = time_unit.lower() in {"s", "sec", "second", "seconds"}
    signals: list[dict[str, Any]] = []

    for name, signal in recording.aux_ts.items():
        warnings: list[str] = []
        time_values = np.asarray([], dtype=np.float64)
        if "time" not in signal.coords:
            warnings.append("缺少 time 坐标")
        else:
            try:
                time_values = np.asarray(signal.time.values, dtype=np.float64)
            except (TypeError, ValueError):
                warnings.append("time 坐标不是数值")
        finite_time = time_values[np.isfinite(time_values)]
        sampling_interval: float | None = None
        sample_rate: float | None = None
        sampling_is_uniform: bool | None = None
        if finite_time.size >= 2:
            steps = np.diff(finite_time)
            if np.any(steps <= 0):
                warnings.append("time 坐标不是严格递增")
            else:
                sampling_interval = _finite_number(np.median(steps))
                if time_is_seconds and sampling_interval is not None:
                    sample_rate = _finite_number(1.0 / sampling_interval)
                elif not time_is_seconds:
                    warnings.append("time 单位不是秒，未计算 Hz 采样率")
                sampling_is_uniform = bool(
                    np.allclose(steps, sampling_interval, rtol=0.01, atol=1e-9)
                )
        elif time_values.size:
            warnings.append("时间采样点少于两个")
        else:
            warnings.append("没有有限的时间采样点")

        finite_fraction: float | None = None
        finite_samples: int | None = None
        missing_samples: int | None = None
        try:
            values = _values(signal)
            finite_samples = int(np.count_nonzero(np.isfinite(values)))
            missing_samples = int(values.size - finite_samples)
            if values.size:
                finite_fraction = _finite_number(finite_samples / values.size)
        except (TypeError, ValueError):
            warnings.append("信号值不是数值")

        start = _finite_number(np.min(finite_time)) if finite_time.size else None
        end = _finite_number(np.max(finite_time)) if finite_time.size else None
        time_offset: float | None = None
        raw_time_offset = signal.attrs.get("time_offset")
        if raw_time_offset is not None:
            try:
                time_offset = _finite_number(raw_time_offset)
            except (TypeError, ValueError):
                warnings.append("time_offset 不是数值")
        signals.append(
            {
                "name": str(name),
                "dimensions": [str(dimension) for dimension in signal.dims],
                "shape": [int(signal.sizes[dimension]) for dimension in signal.dims],
                "unit": _unit_text(signal),
                "samples": int(signal.sizes.get("time", 0)),
                "auxiliary_channel_count": int(signal.sizes.get("aux_channel", 1)),
                "channel_labels": (
                    [str(value) for value in signal.aux_channel.values]
                    if "aux_channel" in signal.coords
                    else []
                ),
                "finite_samples": finite_samples,
                "missing_samples": missing_samples,
                "sample_rate_hz": sample_rate,
                "sampling_is_uniform": sampling_is_uniform,
                "finite_fraction": finite_fraction,
                "time": {
                    "unit": time_unit,
                    "time_offset": time_offset,
                    "available": bool(finite_time.size),
                    "start": start,
                    "end": end,
                    "duration": (
                        _finite_number(end - start)
                        if start is not None and end is not None
                        else None
                    ),
                    "sampling_interval": sampling_interval,
                    "sample_rate_hz": sample_rate,
                    "is_uniform": sampling_is_uniform,
                    "covers_fnirs_recording": (
                        start is not None
                        and end is not None
                        and fnirs_start is not None
                        and fnirs_end is not None
                        and start <= fnirs_start
                        and end >= fnirs_end
                    ),
                },
                "warnings": warnings,
            }
        )

    return {
        "available": bool(signals),
        "count": len(signals),
        "signals": signals,
        "used_for_analysis": False,
        "regression_applied": False,
        "regression_reason": "辅助信号仅做清单；当前任务 GLM 不会自动将其作为回归量。",
    }


def _auxiliary_component_labels(name: str, signal: xr.DataArray, count: int) -> list[str]:
    """Make every selected aux component traceable in the GLM manifest."""
    dimensions = [dimension for dimension in signal.dims if dimension != "time"]
    if len(dimensions) == 1 and dimensions[0] in signal.coords:
        values = [str(value) for value in signal.coords[dimensions[0]].values]
        if len(values) == count:
            return [f"{name}/{value}" for value in values]
    if count == 1:
        return [name]
    return [f"{name}/{index + 1}" for index in range(count)]


def _resample_auxiliary_regressors(
    auxiliary_timeseries: dict[str, xr.DataArray],
    target: xr.DataArray,
    config: AnalysisConfig,
    time_unit: str = "s",
) -> tuple[Any | None, dict[str, Any]]:
    """Build auditable common GLM regressors from explicitly named aux streams.

    The stored `time_offset` is deliberately not applied implicitly. Its semantic
    convention varies by acquisition system, so a nonzero value is reported and
    rejected rather than silently shifting a physiological trace.
    """
    requested = list(config.glm_auxiliary_signal_names)
    status: dict[str, Any] = {
        "requested": config.glm_nuisance_mode in {"auxiliary", "auxiliary_global"},
        "requested_signal_names": requested,
        "available_signal_names": sorted(str(name) for name in auxiliary_timeseries),
        "resampling": {
            "method": "linear",
            "target_time_axis": "fNIRS GLM time",
            "max_gap_seconds": config.glm_auxiliary_max_gap_seconds,
            "standardization": "zscore_after_resampling",
            "time_offset_policy": "reject_nonzero",
            "recording_time_unit": time_unit,
            "anti_aliasing": "lowpass_before_downsampling_when_safe",
        },
        "applied": False,
        "used_regressors": [],
        "rejected": [],
        "reason": "未请求辅助生理回归",
    }
    if not status["requested"]:
        return None, status
    if time_unit.lower() not in {"s", "sec", "second", "seconds"}:
        status["reason"] = "记录 TimeUnit 不是秒，未将辅助流用于 GLM"
        return None, status
    if not requested:
        status["reason"] = "未设置 FNIRS_GLM_AUXILIARY_SIGNALS，未选择辅助流"
        return None, status

    target_time = np.asarray(target.time.values, dtype=np.float64)
    if (
        target_time.size < 2
        or not np.all(np.isfinite(target_time))
        or np.any(np.diff(target_time) <= 0)
    ):
        status["reason"] = "fNIRS GLM 时间轴无效，未进行辅助流对齐"
        return None, status
    target_steps = np.diff(target_time)
    target_rate = float(1.0 / np.median(target_steps))

    regressor_values: list[np.ndarray] = []
    regressor_names: list[str] = []
    for name in requested:
        signal = auxiliary_timeseries.get(name)
        if signal is None:
            status["rejected"].append({"name": name, "reason": "记录中不存在该辅助流"})
            continue
        if "time" not in signal.dims or "time" not in signal.coords:
            status["rejected"].append({"name": name, "reason": "缺少 time 维度或坐标"})
            continue
        raw_offset = signal.attrs.get("time_offset")
        if raw_offset is not None:
            try:
                offset = float(raw_offset)
            except (TypeError, ValueError):
                status["rejected"].append(
                    {"name": name, "reason": "time_offset 不是有限数值"}
                )
                continue
            if not math.isfinite(offset) or not math.isclose(offset, 0.0, abs_tol=1e-9):
                status["rejected"].append(
                    {
                        "name": name,
                        "reason": "存在非零 time_offset，当前策略不会自动平移辅助时间轴",
                        "time_offset": _finite_number(offset),
                    }
                )
                continue
        try:
            source_time = np.asarray(signal.time.values, dtype=np.float64)
            values = _values(signal)
            time_axis = signal.get_axis_num("time")
            values = np.moveaxis(values, time_axis, 0).reshape(source_time.size, -1)
        except (TypeError, ValueError) as exc:
            status["rejected"].append({"name": name, "reason": f"无法读取数值：{exc}"})
            continue
        if (
            source_time.size < 2
            or values.shape[0] != source_time.size
            or not np.all(np.isfinite(source_time))
            or np.any(np.diff(source_time) <= 0)
        ):
            status["rejected"].append(
                {"name": name, "reason": "时间轴必须至少有两个严格递增的有限采样点"}
            )
            continue

        source_steps = np.diff(source_time)
        source_interval = float(np.median(source_steps))
        source_rate = float(1.0 / source_interval)
        source_is_uniform = bool(
            np.allclose(source_steps, source_interval, rtol=0.01, atol=1e-9)
        )
        requires_antialiasing = source_rate > target_rate * 1.01
        antialias_cutoff: float | None = None
        if requires_antialiasing:
            antialias_cutoff = 0.45 * target_rate
            if not source_is_uniform:
                status["rejected"].append(
                    {
                        "name": name,
                        "reason": "高采样率辅助流时间轴不均匀，无法安全执行抗混叠低通",
                        "source_sample_rate_hz": _finite_number(source_rate),
                        "target_sample_rate_hz": _finite_number(target_rate),
                    }
                )
                continue
            if source_time.size < 16 or not np.all(np.isfinite(values)):
                status["rejected"].append(
                    {
                        "name": name,
                        "reason": "高采样率辅助流缺少安全抗混叠所需的完整样本",
                        "source_sample_rate_hz": _finite_number(source_rate),
                        "target_sample_rate_hz": _finite_number(target_rate),
                    }
                )
                continue
            try:
                sos = butter(
                    4,
                    antialias_cutoff,
                    btype="lowpass",
                    fs=source_rate,
                    output="sos",
                )
                values = sosfiltfilt(sos, values, axis=0)
            except ValueError as exc:
                status["rejected"].append(
                    {"name": name, "reason": f"抗混叠低通失败：{exc}"}
                )
                continue

        component_names = _auxiliary_component_labels(name, signal, values.shape[1])
        used_components = 0
        for index, component_name in enumerate(component_names):
            source_values = values[:, index]
            finite = np.isfinite(source_values)
            finite_time = source_time[finite]
            finite_values = source_values[finite]
            component_status: dict[str, Any] = {
                "name": component_name,
                "source_samples": int(source_time.size),
                "finite_samples": int(np.count_nonzero(finite)),
                "source_sample_rate_hz": _finite_number(source_rate),
                "target_sample_rate_hz": _finite_number(target_rate),
                "source_sampling_is_uniform": source_is_uniform,
                "anti_aliasing": {
                    "applied": requires_antialiasing,
                    "method": "4th-order Butterworth lowpass" if requires_antialiasing else None,
                    "cutoff_hz": _finite_number(antialias_cutoff) if antialias_cutoff is not None else None,
                },
            }
            if finite_time.size < 2:
                component_status["reason"] = "有限值少于两个"
                status["rejected"].append(component_status)
                continue
            coverage_tolerance = max(1e-9, float(np.median(target_steps)) * 1e-6)
            if (
                target_time[0] < finite_time[0] - coverage_tolerance
                or target_time[-1] > finite_time[-1] + coverage_tolerance
            ):
                component_status["reason"] = "辅助流未完整覆盖 fNIRS GLM 时间范围"
                status["rejected"].append(component_status)
                continue
            insertion = np.searchsorted(finite_time, target_time, side="left")
            right = np.clip(insertion, 0, finite_time.size - 1)
            left = np.clip(right - 1, 0, finite_time.size - 1)
            bracket_gap = finite_time[right] - finite_time[left]
            if np.any(
                bracket_gap > config.glm_auxiliary_max_gap_seconds + 1e-9
            ):
                component_status["reason"] = (
                    "辅助流缺口超过 "
                    f"{config.glm_auxiliary_max_gap_seconds:g} 秒"
                )
                component_status["maximum_bracket_gap_seconds"] = _finite_number(
                    np.max(bracket_gap)
                )
                status["rejected"].append(component_status)
                continue
            resampled = np.interp(target_time, finite_time, finite_values)
            standard_deviation = float(np.std(resampled))
            if not math.isfinite(standard_deviation) or standard_deviation <= np.finfo(float).eps:
                component_status["reason"] = "重采样后没有可用于回归的方差"
                status["rejected"].append(component_status)
                continue
            resampled = (resampled - float(np.mean(resampled))) / standard_deviation
            component_status.update(
                {
                    "unit": _unit_text(signal),
                    "resampled_samples": int(resampled.size),
                    "source_start_seconds": _finite_number(finite_time[0]),
                    "source_end_seconds": _finite_number(finite_time[-1]),
                    "maximum_bracket_gap_seconds": _finite_number(np.max(bracket_gap)),
                }
            )
            status["used_regressors"].append(component_status)
            regressor_names.append(f"Aux {component_name}")
            regressor_values.append(resampled)
            used_components += 1
        if used_components == 0 and not any(item["name"] == name for item in status["rejected"]):
            status["rejected"].append({"name": name, "reason": "没有可用辅助分量"})

    if not regressor_values:
        status["reason"] = "没有通过时间对齐和完整性校验的辅助回归量"
        return None, status

    chromophores = target.chromo.values
    values = np.stack(regressor_values, axis=1)
    values = np.repeat(values[:, :, np.newaxis], len(chromophores), axis=2)
    regressors = xr.DataArray(
        values,
        dims=("time", "regressor", "chromo"),
        coords={
            "time": target.time.values,
            "regressor": regressor_names,
            "chromo": chromophores,
        },
    )
    status["applied"] = True
    status["reason"] = "已将通过校验的辅助流线性重采样并标准化后加入 GLM"
    return glm.design_matrix.DesignMatrix(common=regressors, channel_wise=[]), status


def _global_mean_regressor(
    target: xr.DataArray,
    config: AnalysisConfig,
) -> tuple[Any | None, dict[str, Any]]:
    requested = config.glm_nuisance_mode in {"global", "auxiliary_global"}
    status: dict[str, Any] = {
        "requested": requested,
        "applied": False,
        "channels": int(target.sizes.get("channel", 0)),
        "method": "Cedalion global mean across modeled channels",
        "self_included": True,
        "reason": "未请求全局回归",
    }
    if not requested:
        return None, status
    if target.sizes.get("channel", 0) < 2:
        status["reason"] = "建模通道少于两个，未建立全局平均回归量"
        return None, status
    status["applied"] = True
    status["reason"] = "已将建模通道的 Cedalion 全局平均加入 GLM"
    return glm.design_matrix.global_mean_regressor(target), status


def _nuisance_regression_status(
    short_separation: dict[str, Any],
    auxiliary_signals: dict[str, Any],
    config: AnalysisConfig,
) -> dict[str, Any]:
    """Make disabled correction paths explicit in every analysis summary."""
    return {
        "short_separation": {
            "applied": False,
            "available_channels": short_separation["short_channel_count"],
            "reason": "将在任务 GLM 建立后根据配置更新。",
        },
        "auxiliary": {
            "applied": False,
            "available_signals": auxiliary_signals["count"],
            "requested_mode": config.glm_nuisance_mode,
            "reason": "将在任务 GLM 建立后检查辅助流时间轴和完整性。",
        },
        "global": {
            "applied": False,
            "requested_mode": config.glm_nuisance_mode,
            "reason": "将在任务 GLM 建立后根据显式配置更新。",
        },
        "cbsi": {
            "requested_mode": config.cbsi_mode,
            "applied": config.cbsi_mode == "on",
            "scope": (
                "task_average_and_glm"
                if config.cbsi_mode == "on"
                else "continuous_comparison_only"
            ),
            "reason": (
                "已请求在 TDDR 后对任务平均与 GLM 应用 CBSI"
                if config.cbsi_mode == "on"
                else "CBSI 未用于任务统计；仍提供连续信号比较曲线"
            ),
        },
    }


def _quality_rows(
    amplitudes: xr.DataArray,
    channels: list[dict[str, Any]],
    config: AnalysisConfig,
    manual_bad_labels: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snr, _ = quality.snr(amplitudes, snr_thresh=config.snr_threshold)
    sci, _ = quality.sci(
        amplitudes,
        config.sci_window_seconds * cedalion.units.s,
        sci_thresh=config.sci_threshold,
    )
    snr_values = _values(snr)
    sci_values = _values(sci)
    psp_error: str | None = None
    try:
        psp, psp_mask = quality.psp(
            amplitudes,
            config.psp_window_seconds * cedalion.units.s,
            psp_thresh=config.psp_computation_threshold,
        )
        psp_values = np.moveaxis(
            _values(psp),
            psp.get_axis_num("channel"),
            0,
        )
        psp_mask_values = np.moveaxis(
            np.asarray(psp_mask.values, dtype=bool),
            psp_mask.get_axis_num("channel"),
            0,
        )
    except Exception as exc:
        psp_values = np.full((len(channels), 1), np.nan, dtype=np.float64)
        psp_mask_values = np.zeros((len(channels), 1), dtype=bool)
        psp_error = str(exc)
    snr_channel_axis = snr.get_axis_num("channel")
    sci_channel_axis = sci.get_axis_num("channel")
    snr_values = np.moveaxis(snr_values, snr_channel_axis, 0)
    sci_values = np.moveaxis(sci_values, sci_channel_axis, 0)

    rows: list[dict[str, Any]] = []
    for index, channel in enumerate(channels):
        snr_mean = _safe_mean(snr_values[index])
        snr_minimum = _safe_min(snr_values[index])
        sci_mean = _safe_mean(sci_values[index])
        channel_psp_values = psp_values[index]
        channel_psp_values = channel_psp_values[np.isfinite(channel_psp_values)]
        psp_median = (
            _finite_number(np.median(channel_psp_values))
            if channel_psp_values.size
            else None
        )
        psp_clean_fraction = (
            float(np.mean(psp_mask_values[index]))
            if psp_mask_values.shape[1]
            else None
        )
        automatic_passed = bool(
            snr_minimum is not None
            and sci_mean is not None
            and snr_minimum >= config.snr_threshold
            and sci_mean >= config.sci_threshold
            and psp_clean_fraction is not None
            and psp_clean_fraction >= config.psp_min_clean_fraction
            and psp_error is None
        )
        exclusion_reasons: list[str] = []
        if snr_minimum is None:
            exclusion_reasons.append("最低 SNR 不可用")
        elif snr_minimum < config.snr_threshold:
            exclusion_reasons.append(
                f"最低 SNR {snr_minimum:g} < 门限 {config.snr_threshold:g}"
            )
        if sci_mean is None:
            exclusion_reasons.append("平均 SCI 不可用")
        elif sci_mean < config.sci_threshold:
            exclusion_reasons.append(
                f"平均 SCI {sci_mean:g} < 门限 {config.sci_threshold:g}"
            )
        if psp_error is not None:
            exclusion_reasons.append(f"PSP 不可用：{psp_error}")
        elif psp_clean_fraction is None:
            exclusion_reasons.append("PSP 合格窗口比例不可用")
        elif psp_clean_fraction < config.psp_min_clean_fraction:
            exclusion_reasons.append(
                f"PSP 合格窗口比例 {psp_clean_fraction:g} < 门限 {config.psp_min_clean_fraction:g}"
            )
        manually_excluded = channel["label"] in manual_bad_labels
        if manually_excluded:
            exclusion_reasons.append("人工标记为坏通道")
        rows.append(
            {
                **channel,
                "snr": snr_mean,
                "snr_minimum": snr_minimum,
                "sci": sci_mean,
                "psp": psp_median,
                "psp_clean_fraction": psp_clean_fraction,
                "psp_threshold": config.psp_computation_threshold,
                "automatic_passed": automatic_passed,
                "manual_bad": manually_excluded,
                "passed": automatic_passed and not manually_excluded,
                "exclusion_reasons": exclusion_reasons,
            }
        )

    passed_count = sum(row["passed"] for row in rows)
    manual_count = sum(row["manual_bad"] for row in rows)
    channel_psp = np.asarray(
        [row["psp"] for row in rows if row["psp"] is not None],
        dtype=np.float64,
    )
    return rows, {
        "passed_channels": passed_count,
        "total_channels": len(rows),
        "pass_rate": passed_count / len(rows) if rows else 0.0,
        "manual_bad_channels": manual_count,
        "snr_threshold": config.snr_threshold,
        "sci_threshold": config.sci_threshold,
        "sci_window_seconds": config.sci_window_seconds,
        "psp_window_seconds": config.psp_window_seconds,
        "psp_threshold": config.psp_computation_threshold,
        "psp_min_clean_fraction": config.psp_min_clean_fraction,
        "psp_median": (
            _finite_number(np.median(channel_psp)) if channel_psp.size else None
        ),
        "psp_is_diagnostic": False,
        "psp_is_quality_gate": True,
        "psp_available": psp_error is None,
        "psp_error": psp_error,
    }


def _motion_quality(
    amplitudes: xr.DataArray,
) -> tuple[dict[str, Any], list[dict[str, Any]], np.ndarray | None]:
    try:
        gvtd, clean_mask = quality.gvtd(amplitudes)
    except Exception as exc:
        return {
            "available": False,
            "method": "GVTD",
            "error": str(exc),
            "flagged_samples": 0,
            "total_samples": int(amplitudes.sizes.get("time", 0)),
            "flagged_fraction": 0.0,
            "segments": 0,
            "median": None,
            "maximum": None,
            "minimum_flagged": None,
            "unit": "OD/s",
        }, [], None
    values = _values(gvtd)
    clean = np.asarray(clean_mask.values, dtype=bool)
    tainted = ~clean
    time = np.asarray(gvtd.time.values, dtype=np.float64)
    time_steps = np.diff(time)
    time_steps = time_steps[np.isfinite(time_steps) & (time_steps > 0)]
    sample_period = float(np.median(time_steps)) if time_steps.size else 0.0
    segments = [
        {
            "onset": float(start),
            "duration": max(sample_period, float(stop) - float(start) + sample_period),
            "label": "GVTD 异常候选",
        }
        for start, stop in quality.mask_to_segments(clean_mask, value=False)
    ]
    finite = values[np.isfinite(values)]
    flagged_values = values[tainted & np.isfinite(values)]
    return {
        "available": True,
        "method": "GVTD",
        "flagged_samples": int(np.count_nonzero(tainted)),
        "total_samples": int(clean.size),
        "flagged_fraction": (
            float(np.count_nonzero(tainted) / clean.size) if clean.size else 0.0
        ),
        "segments": len(segments),
        "median": _finite_number(np.median(finite)) if finite.size else None,
        "maximum": _finite_number(np.max(finite)) if finite.size else None,
        "minimum_flagged": (
            _finite_number(np.min(flagged_values)) if flagged_values.size else None
        ),
        "unit": "OD/s",
    }, segments, clean


def _series_options(
    wavelengths: list[float],
    chromophores: list[str],
) -> list[dict[str, Any]]:
    wavelength_components = [
        {"value": f"{value:g}", "label": f"{value:g} nm"}
        for value in wavelengths
    ]
    chromo_components = [
        {"value": value, "label": value}
        for value in chromophores
    ]
    return [
        {"kind": "amp", "label": "原始光强", "components": wavelength_components},
        {"kind": "od", "label": "光密度 OD", "components": wavelength_components},
        {"kind": "conc", "label": "HbO / HbR", "components": chromo_components},
        {
            "kind": "conc_filtered",
            "label": "滤波后 HbO / HbR",
            "components": chromo_components,
        },
        {
            "kind": "conc_tddr_filtered",
            "label": "TDDR 校正 + 滤波 HbO / HbR",
            "components": chromo_components,
        },
        {
            "kind": "conc_tddr_cbsi_filtered",
            "label": "TDDR + CBSI 校正 + 滤波 HbO / HbR",
            "components": chromo_components,
        },
        {
            "kind": "conc_wavelet_filtered",
            "label": "Wavelet 校正 + 滤波 HbO / HbR（实验）",
            "components": chromo_components,
        },
    ]


def _normalise_stimulus(recording: Any) -> Any:
    """Give Cedalion's official finger-tapping fixture readable condition names."""
    stim = recording.stim.copy()
    if "trial_type" not in stim.columns:
        return stim
    trial_types = stim["trial_type"].astype(str)
    observed = set(trial_types)
    if {"1.0", "2.0", "3.0"}.issubset(observed):
        stim["trial_type"] = trial_types.map(
            lambda value: OFFICIAL_TAPPING_LABELS.get(value, value)
        )
    else:
        stim["trial_type"] = trial_types
    return stim


def _paired_stimulus_intervals(stim: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Pair labels such as ``wt2S`` / ``wt2E`` into neutral task intervals."""
    empty = stim.iloc[0:0].copy()
    if not {"onset", "duration", "value", "trial_type"}.issubset(stim.columns):
        return empty, []

    open_markers: dict[str, list[dict[str, Any]]] = {}
    rows: list[dict[str, Any]] = []
    intervals: list[dict[str, Any]] = []
    for _, marker in stim.sort_values("onset").iterrows():
        marker_name = str(marker["trial_type"])
        match = re.fullmatch(r"(.+)([SE])", marker_name)
        onset = _finite_number(marker["onset"])
        if match is None or onset is None:
            continue
        condition, boundary = match.groups()
        if boundary == "S":
            open_markers.setdefault(condition, []).append(
                {"onset": onset, "value": marker["value"], "name": marker_name}
            )
            continue
        starts = open_markers.get(condition, [])
        if not starts:
            continue
        start = starts.pop(0)
        duration = onset - float(start["onset"])
        if duration <= 0:
            continue
        rows.append(
            {
                "onset": float(start["onset"]),
                "duration": duration,
                "value": start["value"],
                "trial_type": condition,
            }
        )
        intervals.append(
            {
                "onset": float(start["onset"]),
                "duration": duration,
                "value": condition,
                "label": condition,
                "start_marker": str(start["name"]),
                "end_marker": marker_name,
            }
        )

    if not rows:
        return empty, []
    paired = stim.__class__.from_records(rows, columns=stim.columns)
    return paired.sort_values("onset").reset_index(drop=True), intervals


def _task_stimulus(recording: Any) -> tuple[Any, list[dict[str, Any]]]:
    normalised = _normalise_stimulus(recording)
    paired, intervals = _paired_stimulus_intervals(normalised)
    return (paired, intervals) if intervals else (normalised, [])


def _condition_rows(stim: Any) -> list[dict[str, Any]]:
    if "trial_type" not in stim.columns:
        return []
    values = [str(value) for value in stim["trial_type"].tolist()]
    counts = Counter(values)
    official = [condition for condition in CONDITION_LABELS if counts.get(condition, 0)]
    conditions = official or list(dict.fromkeys(values))
    excluded = {"Marker", "start", "stop", "End", "end"}
    result: list[dict[str, Any]] = []
    for condition in conditions:
        if condition in excluded:
            continue
        condition_stim = stim[stim["trial_type"].astype(str) == condition]
        durations = np.asarray(condition_stim["duration"], dtype=np.float64)
        durations = durations[np.isfinite(durations) & (durations >= 0)]
        result.append(
            {
                "value": condition,
                "label": CONDITION_LABELS.get(condition, condition),
                "count": int(counts[condition]),
                "duration_seconds": (
                    _finite_number(np.median(durations)) if durations.size else 0.0
                ),
            }
        )
    return result


def _build_task_analysis(
    stim: Any,
    concentration_task: xr.DataArray,
    quality_rows: list[dict[str, Any]],
    short_channel_labels: set[str],
    motion_clean_mask: np.ndarray | None,
    config: AnalysisConfig,
) -> tuple[dict[str, Any], xr.DataArray | None, xr.DataArray | None]:
    conditions = _condition_rows(stim)
    quality_passed_labels = [row["label"] for row in quality_rows if row["passed"]]
    short_excluded_labels: list[str] = []
    passed_labels = quality_passed_labels
    if config.short_separation_mode == "exclude":
        short_excluded_labels = [
            label for label in quality_passed_labels if label in short_channel_labels
        ]
        passed_labels = [
            label for label in quality_passed_labels if label not in short_channel_labels
        ]
    epoch_before = config.epoch_before_seconds
    epoch_after = config.epoch_after_seconds
    if conditions:
        trial_types_for_window = [item["value"] for item in conditions]
        task_stim_for_window = stim[stim["trial_type"].isin(trial_types_for_window)]
        onsets = np.asarray(task_stim_for_window["onset"], dtype=np.float64)
        onsets = onsets[np.isfinite(onsets)]
        signal_time = np.asarray(concentration_task.time.values, dtype=np.float64)
        if onsets.size and signal_time.size:
            available_before = float(np.min(onsets) - signal_time[0])
            epoch_before = min(config.epoch_before_seconds, max(0.0, available_before))
        durations = [
            float(item["duration_seconds"])
            for item in conditions
            if item.get("duration_seconds") is not None
        ]
        if durations:
            epoch_after = max(config.epoch_after_seconds, max(durations))
    summary: dict[str, Any] = {
        "available": False,
        "conditions": conditions,
        "motion_correction": (
            "TDDR + CBSI" if config.cbsi_mode == "on" else "TDDR"
        ),
        "cbsi": {
            "mode": config.cbsi_mode,
            "applied": config.cbsi_mode == "on",
            "reason": (
                "任务平均使用 TDDR 后的 CBSI 血氧校正信号"
                if config.cbsi_mode == "on"
                else "任务平均未应用 CBSI"
            ),
        },
        "filter_hz": [config.filter_min_hz, config.filter_max_hz],
        "epoch_seconds": [-epoch_before, epoch_after],
        "baseline_seconds": [-epoch_before, 0.0],
        "response_window_seconds": [
            config.response_start_seconds,
            config.response_end_seconds,
        ],
        "stimulus_duration_seconds": None,
        "quality_passed_channels": len(quality_passed_labels),
        "quality_excluded_channels": len(quality_rows) - len(quality_passed_labels),
        "short_separation": {
            "mode": config.short_separation_mode,
            "threshold_mm": config.short_separation_mm,
            "scope": "task_average_only",
            "identified_channels": len(short_channel_labels),
            "excluded_channels": len(short_excluded_labels),
            "excluded_channel_labels": short_excluded_labels,
            "regression_applied": False,
        },
        "usable_channels": len(passed_labels),
        "excluded_channels": len(quality_rows) - len(passed_labels),
        "gvtd": {
            "mode": config.gvtd_mode,
            "applied": False,
            "excluded_epochs": 0,
            "reason": (
                "仅标记 GVTD 异常候选"
                if config.gvtd_mode == "report"
                else "将在任务片段生成后检查每个试次"
            ),
        },
    }
    if not conditions:
        summary["error"] = "没有找到可识别的任务条件"
        return summary, None, None
    if not passed_labels:
        if quality_passed_labels and short_excluded_labels:
            summary["error"] = "短距离通道排除后没有可用于任务分析的通道"
        else:
            summary["error"] = "没有通过 SNR/SCI 门限的通道"
        return summary, None, None

    trial_types = [item["value"] for item in conditions]
    try:
        epochs = concentration_task.cd.to_epochs(
            stim,
            trial_types,
            before=epoch_before * cedalion.units.s,
            after=epoch_after * cedalion.units.s,
        )
        if epochs.sizes.get("epoch", 0) == 0:
            raise ValueError("任务条件没有产生有效片段")
        if config.gvtd_mode == "exclude_epochs":
            if motion_clean_mask is None:
                summary["gvtd"]["reason"] = "GVTD 不可用，未剔除任务试次"
            else:
                sample_time = np.asarray(concentration_task.time.values, dtype=np.float64)
                task_rows = stim[stim["trial_type"].isin(trial_types)]
                onsets_by_condition: dict[str, list[float]] = defaultdict(list)
                for _, event in task_rows.iterrows():
                    onset = _finite_number(event.get("onset"))
                    if onset is not None:
                        onsets_by_condition[str(event["trial_type"])].append(onset)
                seen_by_condition: dict[str, int] = defaultdict(int)
                keep_epoch = np.ones(epochs.sizes["epoch"], dtype=bool)
                for index, trial_type in enumerate(epochs.trial_type.values):
                    label = str(trial_type)
                    order = seen_by_condition[label]
                    seen_by_condition[label] += 1
                    onsets = onsets_by_condition.get(label, [])
                    if order >= len(onsets):
                        continue
                    window = (sample_time >= onsets[order] - epoch_before) & (
                        sample_time <= onsets[order] + epoch_after
                    )
                    if np.any(window) and not bool(np.all(motion_clean_mask[window])):
                        keep_epoch[index] = False
                excluded_epochs = int(np.count_nonzero(~keep_epoch))
                summary["gvtd"].update(
                    {
                        "applied": True,
                        "excluded_epochs": excluded_epochs,
                        "reason": "已剔除包含 GVTD 异常采样的完整任务试次",
                    }
                )
                epochs = epochs.isel(epoch=np.flatnonzero(keep_epoch))
                if epochs.sizes.get("epoch", 0) == 0:
                    raise ValueError("GVTD 剔除后没有可用任务片段")
        baseline = epochs.sel(reltime=epochs.reltime < 0).mean("reltime")
        epochs = epochs - baseline
        average = epochs.groupby("trial_type").mean("epoch")
        deviation = epochs.groupby("trial_type").std("epoch")
        count_by_condition = {
            str(condition): int(np.count_nonzero(epochs.trial_type.values == condition))
            for condition in average.trial_type.values
        }
        count_array = xr.DataArray(
            [count_by_condition[str(condition)] for condition in average.trial_type.values],
            dims="trial_type",
            coords={"trial_type": average.trial_type},
        )
        standard_error = deviation / np.sqrt(count_array)
        standard_error = standard_error.where(count_array > 1)
        average = average.sel(channel=passed_labels)
        standard_error = standard_error.sel(channel=passed_labels)
        task_stim = stim[stim["trial_type"].isin(trial_types)]
        durations = np.asarray(task_stim["duration"], dtype=np.float64)
        durations = durations[np.isfinite(durations)]
        summary["stimulus_duration_seconds"] = (
            _finite_number(np.median(durations)) if durations.size else 0.0
        )
        available_conditions = {str(value) for value in average.trial_type.values}
        conditions = [
            {
                **item,
                "count": count_by_condition[item["value"]],
            }
            for item in conditions
            if item["value"] in available_conditions
        ]
        summary["conditions"] = conditions
        single_trial = [item["label"] for item in conditions if item["count"] < 2]
        summary["single_trial_conditions"] = single_trial
        if single_trial:
            summary["warning"] = "单次任务区间仅用于查看，不能进行重复试次统计"
        summary["available"] = True
        summary["epochs"] = int(epochs.sizes["epoch"])
        return summary, average, standard_error
    except Exception as exc:
        summary["error"] = str(exc)
        return summary, None, None


def _scalar_number(value: Any) -> float | None:
    """Convert a scalar-like statsmodels result to a JSON-safe number."""
    try:
        values = np.asarray(value, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return None
    if values.size != 1:
        return None
    return _finite_number(values[0])


def _confidence_interval(value: Any) -> list[float | None]:
    try:
        values = np.asarray(value, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return [None, None]
    if values.size < 2:
        return [None, None]
    return [_finite_number(values[0]), _finite_number(values[1])]


def _glm_condition_statistics(result: Any, regressor: str) -> dict[str, Any]:
    """Extract one condition coefficient from a Cedalion GLM result cell."""
    params = result.params
    if regressor not in params.index:
        raise ValueError(f"GLM 结果缺少回归量：{regressor}")
    confidence = result.conf_int().loc[regressor]
    return {
        "beta": _scalar_number(params.loc[regressor]),
        "confidence_interval_95": _confidence_interval(confidence),
        "t_value": _scalar_number(result.tvalues.loc[regressor]),
        "p_value": _scalar_number(result.pvalues.loc[regressor]),
        "degrees_of_freedom": _scalar_number(getattr(result, "df_resid", None)),
        "r_squared": _scalar_number(getattr(result, "rsquared", None)),
    }


def _glm_contrast_statistics(
    result: Any,
    left_regressor: str,
    right_regressor: str,
) -> dict[str, Any]:
    """Test a signed condition contrast in one channel and chromophore."""
    params = result.params
    missing = [
        regressor
        for regressor in (left_regressor, right_regressor)
        if regressor not in params.index
    ]
    if missing:
        raise ValueError("GLM 结果缺少回归量：" + "、".join(missing))
    vector = np.zeros(len(params), dtype=np.float64)
    vector[params.index.get_loc(left_regressor)] = 1.0
    vector[params.index.get_loc(right_regressor)] = -1.0
    tested = result.t_test(vector)
    return {
        "effect": _scalar_number(tested.effect),
        "confidence_interval_95": _confidence_interval(tested.conf_int()),
        "t_value": _scalar_number(tested.tvalue),
        "p_value": _scalar_number(tested.pvalue),
        "degrees_of_freedom": _scalar_number(getattr(result, "df_resid", None)),
        "r_squared": _scalar_number(getattr(result, "rsquared", None)),
    }


def _apply_benjamini_hochberg(
    rows: list[dict[str, Any]],
    group_keys: tuple[str, ...],
) -> None:
    """Add per-group Benjamini-Hochberg q-values without changing p-values."""
    grouped: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        value = row.get("p_value")
        if value is not None and math.isfinite(float(value)):
            grouped[tuple(row.get(key) for key in group_keys)].append(index)
        else:
            row["q_value"] = None

    for indices in grouped.values():
        ordered = sorted(indices, key=lambda index: float(rows[index]["p_value"]))
        count = len(ordered)
        running_minimum = 1.0
        for rank, index in reversed(list(enumerate(ordered, start=1))):
            corrected = float(rows[index]["p_value"]) * count / rank
            running_minimum = min(running_minimum, corrected)
            rows[index]["q_value"] = _finite_number(min(1.0, running_minimum))


def _glm_channel_rows(
    quality_rows: list[dict[str, Any]],
    short_channel_labels: set[str],
    config: AnalysisConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
    """Select GLM targets and possible short-channel nuisance regressors."""
    quality_passed = [row for row in quality_rows if row["passed"]]
    task_rows = quality_passed
    if config.short_separation_mode == "exclude":
        task_rows = [
            row for row in quality_passed if row["label"] not in short_channel_labels
        ]
    short_rows = [
        row for row in quality_passed if row["label"] in short_channel_labels
    ]
    long_rows = [
        row for row in task_rows if row["label"] not in short_channel_labels
    ]

    if config.glm_short_separation_mode == "off":
        return task_rows, short_rows, [], "已按配置关闭短间距回归"
    if long_rows and short_rows:
        return long_rows, short_rows, short_rows, "为每个长通道加入最近的合格短间距通道"
    if not short_rows:
        return task_rows, short_rows, [], "没有通过质量门限的短间距通道"
    return task_rows, short_rows, [], "没有可用于短间距回归的长通道"


def _censor_glm_timepoints(
    target: xr.DataArray,
    design: Any,
    motion_clean_mask: np.ndarray | None,
) -> tuple[xr.DataArray, Any, dict[str, Any]]:
    """Remove GVTD-tainted rows from a target and its complete design matrix.

    The HRF convolution and drift regressors are built on the original regular
    time axis first. Censoring only removes matching rows afterwards, so a
    motion gap cannot shift the stimulus timing or alter the drift basis.
    """
    total_samples = int(target.sizes.get("time", 0))
    status = {
        "applied": False,
        "excluded_samples": 0,
        "retained_samples": total_samples,
        "total_samples": total_samples,
        "retained_fraction": 1.0 if total_samples else 0.0,
    }
    if motion_clean_mask is None:
        status["reason"] = "GVTD 不可用，未剔除 GLM 采样"
        return target, design, status

    clean = np.asarray(motion_clean_mask, dtype=bool).reshape(-1)
    if clean.size != total_samples:
        raise ValueError(
            "GVTD 掩码与 GLM 时间轴长度不一致："
            f"{clean.size} != {total_samples}"
        )
    if not np.any(clean):
        raise ValueError("GVTD 剔除后没有可用于 GLM 的采样")

    keep_indices = np.flatnonzero(clean)
    censored_target = target.isel(time=keep_indices)
    censored_design = design.copy()
    if censored_design.common is not None:
        censored_design.common = censored_design.common.isel(time=keep_indices)
    censored_design.channel_wise = [
        regressor.isel(time=keep_indices)
        for regressor in censored_design.channel_wise
    ]
    excluded_samples = int(clean.size - keep_indices.size)
    status.update(
        {
            "applied": True,
            "excluded_samples": excluded_samples,
            "retained_samples": int(keep_indices.size),
            "retained_fraction": float(keep_indices.size / clean.size),
            "reason": (
                "已从 GLM 目标信号和设计矩阵同步剔除 GVTD 异常采样"
                if excluded_samples
                else "未发现 GVTD 异常采样，GLM 保留全部时间点"
            ),
        }
    )
    return censored_target, censored_design, status


def _validate_glm_design(target: xr.DataArray, design: Any) -> None:
    """Reject non-estimable GLM groups before handing them to statsmodels."""
    for chromo, _channels, group_design in design.iter_computational_groups(target):
        values = np.asarray(group_design.pint.dequantify().values, dtype=np.float64)
        samples, regressors = values.shape
        if not np.all(np.isfinite(values)):
            raise ValueError(f"GLM 设计矩阵含有非有限值（{chromo}）")
        if samples <= regressors:
            raise ValueError(
                f"GLM 保留采样不足以估计回归量（{chromo}："
                f"{samples} 个采样，{regressors} 个回归量）"
            )
        if np.linalg.matrix_rank(values) != regressors:
            raise ValueError(f"GLM 设计矩阵不满秩，无法估计独立效应（{chromo}）")


def _build_glm_analysis(
    stim: Any,
    concentration_tddr: xr.DataArray,
    quality_rows: list[dict[str, Any]],
    short_channel_labels: set[str],
    geo3d: xr.DataArray,
    auxiliary_timeseries: dict[str, xr.DataArray],
    recording_time_unit: str,
    config: AnalysisConfig,
    motion_clean_mask: np.ndarray | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fit a Cedalion task GLM while keeping its assumptions in response metadata.

    The GLM deliberately does not reuse the high-pass epoch-average series.
    OLS uses a low-pass TDDR concentration input, while AR-IRLS keeps the full
    TDDR spectrum for its prewhitening step. Cosine drift regressors model slow
    trends within the fit so coefficients retain the design information.
    """
    conditions = _condition_rows(stim)
    contrasts = [
        {
            "value": f"contrast-{index}",
            "label": f"{left['label']} - {right['label']}",
            "left": left["value"],
            "right": right["value"],
        }
        for index, (left, right) in enumerate(combinations(conditions, 2))
    ]
    target_rows, short_rows, used_short_rows, short_reason = _glm_channel_rows(
        quality_rows,
        short_channel_labels,
        config,
    )
    summary: dict[str, Any] = {
        "available": False,
        "method": "Cedalion task GLM",
        "conditions": conditions,
        "contrasts": contrasts,
        "channel_labels": [row["label"] for row in target_rows],
        "channels": len(target_rows),
        "model": {
            "input": (
                f"TDDR-corrected{' and CBSI-corrected' if config.cbsi_mode == 'on' else ''} "
                "HbO/HbR without frequency filtering before AR-IRLS"
                if config.glm_noise_model == "ar_irls"
                else f"TDDR-corrected{' and CBSI-corrected' if config.cbsi_mode == 'on' else ''} "
                "HbO/HbR, low-pass filtered before OLS"
            ),
            "filter_hz": (
                None
                if config.glm_noise_model == "ar_irls"
                else [0.0, config.filter_max_hz]
            ),
            "hrf_basis": "Gamma",
            "hrf_tau_seconds": 0.0,
            "hrf_sigma_seconds": config.glm_hrf_sigma_seconds,
            "drift": "cosine",
            "drift_cutoff_hz": config.glm_drift_cutoff_hz,
            "noise_model": config.glm_noise_model,
            "ar_order": config.glm_ar_order,
            "confidence_level": 0.95,
            "multiple_comparison": "Benjamini-Hochberg FDR per condition/contrast and chromophore across modeled channels",
            "cbsi": {
                "mode": config.cbsi_mode,
                "applied": config.cbsi_mode == "on",
            },
        },
        "short_separation": {
            "requested_mode": config.glm_short_separation_mode,
            "applied": bool(used_short_rows),
            "candidate_channels": len(short_rows),
            "used_channels": [row["label"] for row in used_short_rows],
            "reason": short_reason,
        },
        "auxiliary": {
            "requested": config.glm_nuisance_mode in {"auxiliary", "auxiliary_global"},
            "applied": False,
            "reason": (
                "将在建立 GLM 时间轴后检查"
                if config.glm_nuisance_mode in {"auxiliary", "auxiliary_global"}
                else "未请求辅助生理回归"
            ),
        },
        "global": {
            "requested": config.glm_nuisance_mode in {"global", "auxiliary_global"},
            "applied": False,
            "self_included": True,
            "reason": (
                "将在建立 GLM 目标通道后检查"
                if config.glm_nuisance_mode in {"global", "auxiliary_global"}
                else "未请求全局回归"
            ),
        },
        "gvtd": {
            "mode": config.gvtd_mode,
            "applied": False,
            "excluded_samples": 0,
            "retained_samples": None,
            "total_samples": None,
            "retained_fraction": None,
            "reason": (
                "将在 GLM 设计矩阵建立后同步剔除 GVTD 异常采样"
                if config.gvtd_mode == "exclude_epochs"
                else "仅标记 GVTD 异常候选"
            ),
        },
        "regressors": [],
    }

    def mark_nuisance_unavailable(reason: str) -> None:
        for key in ("auxiliary", "global"):
            if summary[key]["requested"]:
                summary[key]["applied"] = False
                summary[key]["reason"] = f"GLM 未建立：{reason}"

    if not conditions:
        summary["error"] = "没有可用于 GLM 的任务条件"
        mark_nuisance_unavailable(summary["error"])
        return summary, [], []
    if not target_rows:
        summary["error"] = "没有通过质量门限的 GLM 通道"
        mark_nuisance_unavailable(summary["error"])
        return summary, [], []

    condition_values = [item["value"] for item in conditions]
    task_stim = stim[stim["trial_type"].isin(condition_values)].copy()
    if task_stim.empty:
        summary["error"] = "任务事件不能构成 GLM 设计矩阵"
        mark_nuisance_unavailable(summary["error"])
        return summary, [], []

    target_labels = [row["label"] for row in target_rows]
    try:
        gvtd_has_exclusions = bool(
            config.gvtd_mode == "exclude_epochs"
            and motion_clean_mask is not None
            and np.any(~np.asarray(motion_clean_mask, dtype=bool))
        )
        concentration_glm = concentration_tddr
        if config.glm_noise_model == "ols" or gvtd_has_exclusions:
            concentration_glm = freq_filter(
                concentration_tddr,
                0 * cedalion.units.Hz,
                config.filter_max_hz * cedalion.units.Hz,
            )
        target = concentration_glm.sel(channel=target_labels)
        auxiliary_design, auxiliary_status = _resample_auxiliary_regressors(
            auxiliary_timeseries,
            target,
            config,
            recording_time_unit,
        )
        global_design, global_status = _global_mean_regressor(target, config)
        summary["auxiliary"] = auxiliary_status
        summary["global"] = global_status
        design = glm.design_matrix.hrf_regressors(
            target,
            task_stim,
            glm.Gamma(
                tau=0 * cedalion.units.s,
                sigma=config.glm_hrf_sigma_seconds * cedalion.units.s,
            ),
        )
        drift = glm.design_matrix.drift_cosine_regressors(
            target,
            config.glm_drift_cutoff_hz * cedalion.units.Hz,
        )
        if drift.common is None or drift.common.sizes.get("regressor", 0) == 0:
            drift = glm.design_matrix.drift_legendre_regressors(target, order=0)
            summary["model"]["drift"] = "constant"
            summary["model"]["drift_fallback_reason"] = (
                "记录时长不足以生成余弦漂移项"
            )
        design = design & drift
        if auxiliary_design is not None:
            design = design & auxiliary_design
        if global_design is not None:
            design = design & global_design
        if used_short_rows:
            short = concentration_glm.sel(
                channel=[row["label"] for row in used_short_rows]
            )
            design = design & glm.design_matrix.closest_short_channel_regressor(
                target,
                short,
                geo3d,
            )
        fit_noise_model = config.glm_noise_model
        if config.gvtd_mode == "exclude_epochs":
            target, design, gvtd_fit_status = _censor_glm_timepoints(
                target,
                design,
                motion_clean_mask,
            )
            summary["gvtd"].update(gvtd_fit_status)
            if (
                gvtd_fit_status["excluded_samples"]
                and config.glm_noise_model == "ar_irls"
            ):
                # Removing rows creates an irregular time axis. AR-IRLS assumes
                # adjacent rows are equally spaced, so use the valid OLS path.
                fit_noise_model = "ols"
                summary["model"]["noise_model_fallback"] = (
                    "GVTD 删点后时间轴不规则，AR-IRLS 降级为 OLS"
                )
                summary["model"]["filter_hz"] = [0.0, config.filter_max_hz]
        summary["model"]["requested_noise_model"] = config.glm_noise_model
        summary["model"]["noise_model"] = fit_noise_model
        if fit_noise_model == "ols":
            summary["model"]["input"] = (
                f"TDDR-corrected{' and CBSI-corrected' if config.cbsi_mode == 'on' else ''} "
                "HbO/HbR, low-pass filtered before OLS"
            )
        summary["regressors"] = design.regressors
        _validate_glm_design(target, design)
        results = glm.fit(
            target,
            design,
            noise_model=fit_noise_model,
            ar_order=config.glm_ar_order,
            max_jobs=1,
        )
    except Exception as exc:
        summary["error"] = f"GLM 拟合失败：{exc}"
        mark_nuisance_unavailable(summary["error"])
        return summary, [], []

    condition_effects: list[dict[str, Any]] = []
    contrast_effects: list[dict[str, Any]] = []
    try:
        for channel in target_rows:
            label = channel["label"]
            for chromo in ("HbO", "HbR"):
                result = results.sel(channel=label, chromo=chromo).item()
                for condition in conditions:
                    condition_effects.append(
                        {
                            "channel": channel,
                            "chromo": chromo,
                            "condition": condition["value"],
                            "condition_label": condition["label"],
                            **_glm_condition_statistics(
                                result,
                                f"HRF {condition['value']}",
                            ),
                        }
                    )
                for contrast in contrasts:
                    contrast_effects.append(
                        {
                            "channel": channel,
                            "chromo": chromo,
                            "contrast": contrast["value"],
                            "contrast_label": contrast["label"],
                            "left": contrast["left"],
                            "right": contrast["right"],
                            **_glm_contrast_statistics(
                                result,
                                f"HRF {contrast['left']}",
                                f"HRF {contrast['right']}",
                            ),
                        }
                    )
    except Exception as exc:
        summary["error"] = f"无法读取 GLM 统计量：{exc}"
        return summary, [], []

    _apply_benjamini_hochberg(condition_effects, ("condition", "chromo"))
    _apply_benjamini_hochberg(contrast_effects, ("contrast", "chromo"))
    summary["available"] = True
    return summary, condition_effects, contrast_effects


@lru_cache(maxsize=3)
def _load_analysis_cached(
    filename: str,
    modified_ns: int,
    size_bytes: int,
    config: AnalysisConfig,
    recording_id: str,
    manual_qc_json: str,
) -> AnalysisData:
    path = Path(filename)
    input_sha256 = _file_sha256(filename, size_bytes, modified_ns)
    software = _software_versions()
    manual_qc = json.loads(manual_qc_json)
    analysis_id, analysis_fingerprint = _analysis_identity(
        input_sha256,
        config,
        software,
        manual_qc,
    )
    recordings, subject_metadata, input_compatibility = (
        _read_snirf_with_subject_id_compatibility(path, input_sha256)
    )
    if not recordings:
        raise ValueError("SNIRF 中没有可分析的 NIRS 记录")
    recording = recordings[0]
    if "amp" not in recording.timeseries:
        raise ValueError("SNIRF 中没有原始光强 amp 数据")

    input_validation = _validate_recording_input(
        recording["amp"],
        recording.stim,
        config,
    )
    amplitudes, preprocessing = _prepare_amplitudes(recording["amp"], config)
    amplitudes, unit_metadata = _normalise_amplitude_units(
        amplitudes,
        input_validation,
    )
    geometry_validation = _validate_geometry(amplitudes, recording.geo3d, config)
    short_separation = _short_separation_summary(
        amplitudes,
        recording.geo3d,
        config,
    )
    auxiliary_signals = _auxiliary_signal_inventory(
        recording,
        np.asarray(amplitudes.time.values, dtype=np.float64),
    )
    nuisance_regression = _nuisance_regression_status(
        short_separation,
        auxiliary_signals,
        config,
    )
    validation_warnings = list(
        dict.fromkeys(
            input_validation["warnings"] + geometry_validation["warnings"]
        )
    )

    optical_density = cw.int2od(amplitudes)
    wavelength_count = optical_density.sizes.get("wavelength", 0)
    if wavelength_count < 2:
        raise ValueError("计算 HbO/HbR 至少需要两个波长")
    dpf = xr.DataArray(
        [config.dpf] * wavelength_count,
        dims="wavelength",
        coords={"wavelength": optical_density.wavelength},
    )
    concentration = cw.od2conc(
        optical_density,
        recording.geo3d,
        dpf,
        spectrum="prahl",
    ).pint.to("micromolar")
    concentration_filtered = freq_filter(
        concentration,
        config.filter_min_hz * cedalion.units.Hz,
        config.filter_max_hz * cedalion.units.Hz,
    )
    optical_density_tddr = motion.tddr(optical_density)
    concentration_tddr = cw.od2conc(
        optical_density_tddr,
        recording.geo3d,
        dpf,
        spectrum="prahl",
    ).pint.to("micromolar")
    concentration_tddr_cbsi, cbsi_status = _cbsi_correct(concentration_tddr)
    concentration_tddr_filtered = freq_filter(
        concentration_tddr,
        config.filter_min_hz * cedalion.units.Hz,
        config.filter_max_hz * cedalion.units.Hz,
    )
    concentration_tddr_cbsi_filtered = freq_filter(
        concentration_tddr_cbsi,
        config.filter_min_hz * cedalion.units.Hz,
        config.filter_max_hz * cedalion.units.Hz,
    )
    optical_density_wavelet = motion.wavelet(optical_density)
    concentration_wavelet = cw.od2conc(
        optical_density_wavelet,
        recording.geo3d,
        dpf,
        spectrum="prahl",
    ).pint.to("micromolar")
    concentration_wavelet_filtered = freq_filter(
        concentration_wavelet,
        config.filter_min_hz * cedalion.units.Hz,
        config.filter_max_hz * cedalion.units.Hz,
    )

    channels = _build_channels(amplitudes)
    manual_bad_labels = set(manual_qc["bad_channel_labels"])
    quality_rows, quality_summary = _quality_rows(
        amplitudes, channels, config, manual_bad_labels
    )
    motion_summary, motion_segments, motion_clean_mask = _motion_quality(amplitudes)
    task_stim, intervals = _task_stimulus(recording)
    short_channel_labels = {
        channel["label"] for channel in short_separation["short_channels"]
    }
    cbsi_requested = config.cbsi_mode == "on"
    cbsi_applied = cbsi_requested and cbsi_status["corrected_channels"] > 0
    nuisance_regression["cbsi"] = {
        **cbsi_status,
        "requested_mode": config.cbsi_mode,
        "applied": cbsi_applied,
        "continuous_comparison_available": True,
        "scope": (
            "task_average_and_glm"
            if cbsi_requested
            else "continuous_comparison_only"
        ),
        "reason": (
            "已在 TDDR 后对任务平均与 GLM 应用 CBSI"
            if cbsi_applied
            else (
                "已请求 CBSI，但没有可安全估计标准差比例的通道"
                if cbsi_requested
                else "任务统计未应用 CBSI；连续信号可选择 CBSI 比较曲线"
            )
        ),
    }
    concentration_task = (
        concentration_tddr_cbsi_filtered
        if cbsi_requested
        else concentration_tddr_filtered
    )
    concentration_glm = (
        concentration_tddr_cbsi if cbsi_requested else concentration_tddr
    )
    task_summary, task_average, task_sem = _build_task_analysis(
        task_stim,
        concentration_task,
        quality_rows,
        short_channel_labels,
        motion_clean_mask,
        config,
    )
    glm_summary, glm_condition_effects, glm_contrast_effects = _build_glm_analysis(
        task_stim,
        concentration_glm,
        quality_rows,
        short_channel_labels,
        recording.geo3d,
        recording.aux_ts,
        str(recording.meta_data.get("TimeUnit", "unknown")),
        config,
        motion_clean_mask=motion_clean_mask,
    )
    if cbsi_requested and not cbsi_applied:
        task_summary["motion_correction"] = "TDDR"
        task_summary["cbsi"].update(
            {
                "applied": False,
                "reason": "已请求 CBSI，但没有可安全估计标准差比例的通道",
            }
        )
        glm_summary["model"]["input"] = glm_summary["model"]["input"].replace(
            " and CBSI-corrected", ""
        )
        glm_summary["model"]["cbsi"]["applied"] = False
    auxiliary_signals["used_for_analysis"] = bool(
        glm_summary["auxiliary"]["applied"]
    )
    auxiliary_signals["regression_applied"] = bool(
        glm_summary["auxiliary"]["applied"]
    )
    auxiliary_signals["regression_reason"] = glm_summary["auxiliary"]["reason"]
    task_summary["glm"] = glm_summary
    nuisance_regression["short_separation"] = {
        "applied": glm_summary["short_separation"]["applied"],
        "available_channels": short_separation["short_channel_count"],
        "scope": "task_glm_only",
        "reason": glm_summary["short_separation"]["reason"],
    }
    nuisance_regression["auxiliary"] = {
        **glm_summary["auxiliary"],
        "available_signals": auxiliary_signals["count"],
        "scope": "task_glm_only",
    }
    nuisance_regression["global"] = {
        **glm_summary["global"],
        "scope": "task_glm_only",
    }
    task_short_exclusions = set(
        task_summary["short_separation"]["excluded_channel_labels"]
    )
    for row in quality_rows:
        row["short_separation"] = (
            "short" if row["label"] in short_channel_labels else "long"
        )
        row["task_channel_eligible"] = bool(
            row["passed"] and row["label"] not in task_short_exclusions
        )
    wavelengths = [float(value) for value in amplitudes.wavelength.values]
    chromophores = [str(value) for value in concentration.chromo.values]
    time = np.asarray(amplitudes.time.values, dtype=np.float64)
    time_steps = np.diff(time)
    time_steps = time_steps[np.isfinite(time_steps) & (time_steps > 0)]
    sample_rate = float(1.0 / np.median(time_steps)) if time_steps.size else None
    duration = float(time[-1] - time[0]) if time.size > 1 else 0.0

    normalised_stim = _normalise_stimulus(recording)
    trial_types: list[str] = []
    if "trial_type" in normalised_stim.columns:
        trial_types = [str(value) for value in normalised_stim["trial_type"].tolist()]
    event_counts = [
        {
            "label": CONDITION_LABELS.get(label, "边界标记" if label == "Marker" else label),
            "value": label,
            "count": count,
        }
        for label, count in sorted(Counter(trial_types).items())
    ]
    events: list[dict[str, Any]] = []
    if "onset" in normalised_stim.columns:
        for _, event in normalised_stim.iterrows():
            onset = _finite_number(event.get("onset", 0.0))
            if onset is None:
                continue
            duration_value = _finite_number(event.get("duration", 0.0))
            value = str(event.get("trial_type", "—"))
            events.append(
                {
                    "onset": onset,
                    "duration": duration_value if duration_value is not None else 0.0,
                    "value": value,
                    "label": CONDITION_LABELS.get(
                        value,
                        "边界标记" if value == "Marker" else value,
                    ),
                }
            )
    summary = {
        "filename": path.name,
        "file_size_bytes": size_bytes,
        "subject": subject_metadata,
        "recording": {
            "id": recording_id,
            "filename": path.name,
            "size_bytes": size_bytes,
            "modified_ns": modified_ns,
            "modified_at_utc": _utc_timestamp(modified_ns / 1_000_000_000),
        },
        "analysis": {
            "id": analysis_id,
            "fingerprint_sha256": analysis_fingerprint,
            "manifest_version": ANALYSIS_MANIFEST_VERSION,
            "protocol_version": ANALYSIS_PROTOCOL_VERSION,
            "created_at_utc": _utc_now(),
            "input_sha256": input_sha256,
            "software": software,
        },
        "samples": int(amplitudes.sizes["time"]),
        "channels": int(amplitudes.sizes["channel"]),
        "measurements": int(amplitudes.sizes["channel"] * wavelength_count),
        "duration_seconds": duration,
        "sample_rate_hz": sample_rate,
        "wavelengths_nm": wavelengths,
        "stimulus_events": len(recording.stim),
        "task_intervals": len(intervals),
        "trial_types": [item["label"] for item in event_counts],
        "cedalion_version": cedalion.__version__,
        "dpf": config.dpf,
        "filter_hz": [config.filter_min_hz, config.filter_max_hz],
        "analysis_parameters": config.metadata(),
        "manual_quality_control": manual_qc,
        "short_separation": short_separation,
        "nuisance_regression": nuisance_regression,
        "input_validation": {
            "valid": True,
            "warnings": validation_warnings,
            "compatibility": input_compatibility,
            "amplitude": {
                **{
                    key: value
                    for key, value in input_validation.items()
                    if key not in {"warnings", "stimulus"}
                },
                "analysis_unit": unit_metadata["analysis_unit"],
                "normalised": unit_metadata["normalised"],
            },
            "stimulus": input_validation["stimulus"],
            "geometry": geometry_validation,
            "auxiliary": auxiliary_signals,
        },
        **preprocessing,
    }
    series = {
        "amp": amplitudes,
        "od": optical_density,
        "conc": concentration,
        "conc_filtered": concentration_filtered,
        "conc_tddr_filtered": concentration_tddr_filtered,
        "conc_tddr_cbsi_filtered": concentration_tddr_cbsi_filtered,
        "conc_wavelet_filtered": concentration_wavelet_filtered,
    }
    return AnalysisData(
        summary=summary,
        config=config,
        channels=channels,
        series_options=_series_options(wavelengths, chromophores),
        event_counts=event_counts,
        events=events,
        intervals=intervals,
        quality=quality_rows,
        quality_summary=quality_summary,
        motion_summary=motion_summary,
        motion_segments=motion_segments,
        motion_clean_mask=motion_clean_mask,
        series=series,
        task_summary=task_summary,
        task_average=task_average,
        task_sem=task_sem,
        glm_summary=glm_summary,
        glm_condition_effects=glm_condition_effects,
        glm_contrast_effects=glm_contrast_effects,
    )


def load_analysis(
    path: Path,
    config: AnalysisConfig | None = None,
    recording_id: str | None = None,
    qc_decisions: dict[str, Any] | None = None,
) -> AnalysisData:
    path = path.resolve()
    stat = path.stat()
    config = config or AnalysisConfig.from_environment()
    recording_id = recording_id or path.name
    qc_decisions = qc_decisions or {"bad_channel_labels": [], "updated_at_utc": None}
    manual_qc_json = json.dumps(qc_decisions, ensure_ascii=True, sort_keys=True)
    # The dashboard requests summary and quality data concurrently. Serialize the
    # first cache miss so both requests share one Cedalion/GLM calculation.
    with ANALYSIS_LOAD_LOCK:
        return _load_analysis_cached(
            str(path),
            stat.st_mtime_ns,
            stat.st_size,
            config,
            recording_id,
            manual_qc_json,
        )


def signal_payload(
    analysis: AnalysisData,
    kind: str,
    channel_index: int,
    component: str | None,
    max_points: int,
) -> dict[str, Any]:
    if kind not in analysis.series:
        raise ValueError(f"不支持的分析类型：{kind}")
    if channel_index < 0 or channel_index >= len(analysis.channels):
        raise IndexError("通道编号超出范围")

    data = analysis.series[kind]
    channel = analysis.channels[channel_index]
    selected = data.sel(channel=channel["label"])
    if "wavelength" in selected.dims:
        target = float(component) if component else float(selected.wavelength.values[0])
        selected = selected.sel(wavelength=target, method="nearest")
        component_label = f"{float(selected.wavelength.item()):g} nm"
    elif "chromo" in selected.dims:
        target = component or str(selected.chromo.values[0])
        if target not in {str(value) for value in selected.chromo.values}:
            raise ValueError(f"不支持的血红蛋白类型：{target}")
        selected = selected.sel(chromo=target)
        component_label = target
    else:
        component_label = component or ""

    values = _values(selected)
    time = np.asarray(selected.time.values, dtype=np.float64)
    if values.ndim != 1 or time.ndim != 1:
        raise ValueError("选择结果不是一维时间序列")

    max_points = max(200, min(max_points, 5000))
    stride = max(1, math.ceil(values.size / max_points))
    sampled_time = time[::stride]
    sampled_values = values[::stride]
    finite = values[np.isfinite(values)]
    stats = {
        "minimum": _finite_number(np.min(finite)) if finite.size else None,
        "maximum": _finite_number(np.max(finite)) if finite.size else None,
        "mean": _finite_number(np.mean(finite)) if finite.size else None,
        "stddev": _finite_number(np.std(finite)) if finite.size else None,
    }
    points = [
        [float(t), _finite_number(value)]
        for t, value in zip(sampled_time, sampled_values)
        if math.isfinite(float(t))
    ]
    option = next(item for item in analysis.series_options if item["kind"] == kind)
    return {
        "series": {
            "kind": kind,
            "label": option["label"],
            "component": component_label,
            "channel": channel,
            "unit": _display_unit(selected, kind),
        },
        "stats": stats,
        "stride": stride,
        "points": points,
    }


def _task_condition(
    analysis: AnalysisData,
    condition: str | None,
) -> dict[str, Any]:
    conditions = analysis.task_summary.get("conditions", [])
    if not conditions:
        raise ValueError(analysis.task_summary.get("error", "没有可用任务条件"))
    selected = condition or conditions[0]["value"]
    for item in conditions:
        if item["value"] == selected:
            return item
    raise ValueError(f"不支持的任务条件：{selected}")


def _task_channel(analysis: AnalysisData, channel_index: int) -> dict[str, Any]:
    if channel_index < 0 or channel_index >= len(analysis.quality):
        raise IndexError("通道编号超出范围")
    channel = analysis.quality[channel_index]
    if not channel["passed"]:
        raise ValueError(f"通道 {channel['label']} 未通过质量门限，已从任务分析排除")
    short_exclusions = set(
        analysis.task_summary.get("short_separation", {}).get(
            "excluded_channel_labels",
            [],
        )
    )
    if channel["label"] in short_exclusions:
        raise ValueError(
            f"通道 {channel['label']} 是短距离通道，已按 "
            "FNIRS_SHORT_SEPARATION_MODE=exclude 从任务分析排除"
        )
    return channel


def _task_arrays(
    analysis: AnalysisData,
    condition: str | None,
    channel_index: int,
) -> tuple[dict[str, Any], dict[str, Any], np.ndarray, dict[str, np.ndarray]]:
    if analysis.task_average is None or analysis.task_sem is None:
        raise ValueError(analysis.task_summary.get("error", "任务分析不可用"))
    condition_row = _task_condition(analysis, condition)
    channel = _task_channel(analysis, channel_index)
    average = analysis.task_average.sel(
        trial_type=condition_row["value"],
        channel=channel["label"],
    )
    standard_error = analysis.task_sem.sel(
        trial_type=condition_row["value"],
        channel=channel["label"],
    )
    time = np.asarray(average.reltime.values, dtype=np.float64)
    arrays: dict[str, np.ndarray] = {}
    for chromo in ("HbO", "HbR"):
        arrays[chromo] = _values(average.sel(chromo=chromo))
        arrays[f"{chromo}_sem"] = _values(standard_error.sel(chromo=chromo))
    return condition_row, channel, time, arrays


def task_response_payload(
    analysis: AnalysisData,
    condition: str | None,
    channel_index: int,
    max_points: int,
) -> dict[str, Any]:
    condition_row, channel, time, arrays = _task_arrays(
        analysis,
        condition,
        channel_index,
    )
    max_points = max(100, min(max_points, 2000))
    stride = max(1, math.ceil(time.size / max_points))
    response_start = analysis.config.response_start_seconds
    response_end = analysis.config.response_end_seconds
    response_mask = (
        np.isfinite(time) & (time >= response_start) & (time <= response_end)
    )

    def metric(values: np.ndarray, mode: str) -> dict[str, float | None]:
        valid = response_mask & np.isfinite(values)
        if not np.any(valid):
            return {"amplitude": None, "latency_seconds": None}
        indices = np.flatnonzero(valid)
        target_index = indices[np.argmax(values[valid]) if mode == "max" else np.argmin(values[valid])]
        return {
            "amplitude": _finite_number(values[target_index]),
            "latency_seconds": _finite_number(time[target_index]),
        }

    series = []
    for chromo in ("HbO", "HbR"):
        points = [
            [float(t), _finite_number(value), _finite_number(error)]
            for t, value, error in zip(
                time[::stride],
                arrays[chromo][::stride],
                arrays[f"{chromo}_sem"][::stride],
            )
            if math.isfinite(float(t))
        ]
        series.append({"name": chromo, "points": points})
    return {
        "condition": condition_row,
        "channel": channel,
        "unit": "µM",
        "stimulus": {
            "onset_seconds": 0.0,
            "duration_seconds": condition_row.get(
                "duration_seconds",
                analysis.task_summary["stimulus_duration_seconds"],
            ),
        },
        "response_window_seconds": [response_start, response_end],
        "metrics": {
            "hbo_peak": metric(arrays["HbO"], "max"),
            "hbr_trough": metric(arrays["HbR"], "min"),
        },
        "stride": stride,
        "series": series,
    }


def analysis_metadata_payload(analysis: AnalysisData) -> dict[str, Any]:
    """Return the complete, JSON-safe manifest for one cached analysis run."""
    summary = analysis.summary
    analysis_info = summary["analysis"]
    task_summary = {
        key: value for key, value in analysis.task_summary.items() if key != "glm"
    }
    quality_exclusions = [
        {
            "index": row["index"],
            "label": row["label"],
            "source": row["source"],
            "detector": row["detector"],
            "snr_minimum": row["snr_minimum"],
            "sci": row["sci"],
            "reasons": row["exclusion_reasons"],
        }
        for row in analysis.quality
        if not row["passed"]
    ]
    return {
        "manifest_version": ANALYSIS_MANIFEST_VERSION,
        "analysis_id": analysis_info["id"],
        "analysis_fingerprint_sha256": analysis_info["fingerprint_sha256"],
        "created_at_utc": analysis_info["created_at_utc"],
        "filename": summary["filename"],
        "cedalion_version": summary["cedalion_version"],
        "recording": summary["recording"],
        "input": {
            **summary["recording"],
            "sha256": analysis_info["input_sha256"],
        },
        "software": analysis_info["software"],
        "parameters": summary["analysis_parameters"],
        "validation": summary["input_validation"],
        "subject": summary["subject"],
        "preprocessing": {
            "raw_channels": summary["raw_channels"],
            "analyzed_channels": summary["analyzed_channels"],
            "excluded_nonpositive_channels": summary[
                "excluded_nonpositive_channels"
            ],
            "excluded_nonpositive_channel_labels": summary[
                "excluded_nonpositive_channel_labels"
            ],
            "interpolated_samples": summary["interpolated_samples"],
            "minimum_positive_fraction": summary["minimum_positive_fraction"],
        },
        "quality_control": {
            "summary": analysis.quality_summary,
            "manual_decisions": summary["manual_quality_control"],
            "excluded_channels": quality_exclusions,
            "task_short_separation_exclusions": analysis.task_summary.get(
                "short_separation",
                {},
            ),
            "motion": analysis.motion_summary,
        },
        "short_separation": summary["short_separation"],
        "nuisance_regression": summary["nuisance_regression"],
        "task": task_summary,
        "task_glm": analysis.glm_summary,
    }


def analysis_metadata_bytes(analysis: AnalysisData) -> tuple[bytes, str]:
    payload = analysis_metadata_payload(analysis)
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2).encode(
        "utf-8"
    )
    recording_slug = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        payload["recording"]["id"],
    ).strip("-")
    filename = f"analysis-manifest-{recording_slug}-{payload['analysis_id']}.json"
    return body, filename


def task_csv_bytes(
    analysis: AnalysisData,
    condition: str | None,
    channel_index: int,
) -> tuple[bytes, str]:
    condition_row, channel, time, arrays = _task_arrays(
        analysis,
        condition,
        channel_index,
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    analysis_info = analysis.summary["analysis"]
    recording_id = analysis.summary["recording"]["id"]
    writer.writerow(
        [
            "analysis_id",
            "input_sha256",
            "recording_id",
            "relative_time_s",
            "HbO_uM",
            "HbO_SEM_uM",
            "HbR_uM",
            "HbR_SEM_uM",
        ]
    )
    for values in zip(
        time,
        arrays["HbO"],
        arrays["HbO_sem"],
        arrays["HbR"],
        arrays["HbR_sem"],
    ):
        writer.writerow(
            [
                analysis_info["id"],
                analysis_info["input_sha256"],
                recording_id,
                *[
                    "" if not math.isfinite(float(value)) else float(value)
                    for value in values
                ],
            ]
        )
    download_slug = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        f"{condition_row['value']}-{channel['label']}",
    ).strip("-")
    filename = f"task-{download_slug}-{analysis_info['id']}.csv"
    return output.getvalue().encode("utf-8-sig"), filename


def _glm_condition(
    analysis: AnalysisData,
    condition: str | None,
) -> dict[str, Any]:
    conditions = analysis.glm_summary.get("conditions", [])
    if not conditions:
        raise ValueError(analysis.glm_summary.get("error", "没有可用 GLM 条件"))
    selected = condition or conditions[0]["value"]
    for item in conditions:
        if item["value"] == selected:
            return item
    raise ValueError(f"不支持的 GLM 条件：{selected}")


def _glm_channel(analysis: AnalysisData, channel_index: int) -> dict[str, Any]:
    if channel_index < 0 or channel_index >= len(analysis.quality):
        raise IndexError("通道编号超出范围")
    channel = analysis.quality[channel_index]
    modelled = set(analysis.glm_summary.get("channel_labels", []))
    if channel["label"] not in modelled:
        raise ValueError(f"通道 {channel['label']} 未纳入当前 GLM")
    return channel


def _glm_contrast(
    analysis: AnalysisData,
    contrast: str | None,
) -> dict[str, Any] | None:
    contrasts = analysis.glm_summary.get("contrasts", [])
    if not contrasts:
        return None
    selected = contrast or contrasts[0]["value"]
    for item in contrasts:
        if item["value"] == selected:
            return item
    raise ValueError(f"不支持的 GLM 条件对比：{selected}")


def task_glm_payload(
    analysis: AnalysisData,
    condition: str | None,
    channel_index: int,
    contrast: str | None,
) -> dict[str, Any]:
    if not analysis.glm_summary.get("available"):
        raise ValueError(analysis.glm_summary.get("error", "GLM 统计不可用"))
    condition_row = _glm_condition(analysis, condition)
    channel = _glm_channel(analysis, channel_index)
    contrast_row = _glm_contrast(analysis, contrast)
    condition_effects = [
        row
        for row in analysis.glm_condition_effects
        if row["channel"]["label"] == channel["label"]
        and row["condition"] == condition_row["value"]
    ]
    if not condition_effects:
        raise ValueError("找不到所选通道和条件的 GLM 统计量")
    contrast_effects: list[dict[str, Any]] = []
    if contrast_row is not None:
        contrast_effects = [
            row
            for row in analysis.glm_contrast_effects
            if row["channel"]["label"] == channel["label"]
            and row["contrast"] == contrast_row["value"]
        ]
    return {
        "summary": analysis.glm_summary,
        "channel": channel,
        "condition": condition_row,
        "condition_effects": condition_effects,
        "contrast": contrast_row,
        "contrast_effects": contrast_effects,
    }


def task_glm_csv_bytes(analysis: AnalysisData) -> tuple[bytes, str]:
    if not analysis.glm_summary.get("available"):
        raise ValueError(analysis.glm_summary.get("error", "GLM 统计不可用"))
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    analysis_info = analysis.summary["analysis"]
    recording_id = analysis.summary["recording"]["id"]
    writer.writerow(
        [
            "analysis_id",
            "input_sha256",
            "recording_id",
            "result_type",
            "channel_index",
            "channel",
            "source",
            "detector",
            "chromophore",
            "name",
            "left_condition",
            "right_condition",
            "estimate_uM",
            "ci95_lower_uM",
            "ci95_upper_uM",
            "t_value",
            "p_value",
            "q_value_bh_fdr",
            "degrees_of_freedom",
            "r_squared",
        ]
    )

    def write_effect(row: dict[str, Any], result_type: str) -> None:
        confidence = row["confidence_interval_95"]
        channel = row["channel"]
        writer.writerow(
            [
                analysis_info["id"],
                analysis_info["input_sha256"],
                recording_id,
                result_type,
                channel["index"],
                channel["label"],
                channel["source"],
                channel["detector"],
                row["chromo"],
                row.get("condition_label") or row.get("contrast_label"),
                row.get("left", ""),
                row.get("right", ""),
                row.get("beta", row.get("effect")),
                confidence[0],
                confidence[1],
                row["t_value"],
                row["p_value"],
                row.get("q_value"),
                row["degrees_of_freedom"],
                row["r_squared"],
            ]
        )

    for row in analysis.glm_condition_effects:
        write_effect(row, "condition")
    for row in analysis.glm_contrast_effects:
        write_effect(row, "contrast")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", analysis.summary["filename"]).strip("-")
    return (
        output.getvalue().encode("utf-8-sig"),
        f"glm-{slug}-{analysis_info['id']}.csv",
    )


class DashboardHandler(SimpleHTTPRequestHandler):
    data_dir: Path
    default_data_file: Path
    analysis_config: AnalysisConfig
    environment_config: AnalysisConfig
    analysis_config_source: str
    analysis_config_store: AnalysisConfigStore
    qc_decision_store: QcDecisionStore

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self) -> None:  # noqa: N802
        # Static assets are small and versioned; avoid serving a stale page after deployment.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error_json(self, status: HTTPStatus, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _request_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > 64 * 1024:
            raise ValueError("请求 JSON 必须在 1 到 65536 字节之间")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 JSON 对象") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _load_selected_analysis(
        self,
        data_file: Path,
        recording_id: str,
    ) -> AnalysisData:
        return load_analysis(
            data_file,
            self.analysis_config,
            recording_id,
            self.qc_decision_store.for_recording(recording_id),
        )

    @classmethod
    def _current_config(cls) -> tuple[AnalysisConfig, str]:
        with ANALYSIS_CONFIG_LOCK:
            return cls.analysis_config, cls.analysis_config_source

    def _bytes(
        self,
        body: bytes,
        content_type: str,
        filename: str | None = None,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _single_query_value(
        query: dict[str, list[str]],
        name: str,
    ) -> str | None:
        values = query.get(name, [])
        if not values:
            return None
        if len(values) != 1:
            raise ValueError(f"{name} 参数只能提供一次")
        return values[0]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            page_routes = {
                "/": "/index.html",
                "/signals": "/signals.html",
                "/signals/": "/signals.html",
                "/task": "/task.html",
                "/task/": "/task.html",
                "/quality": "/quality.html",
                "/quality/": "/quality.html",
            }
            if parsed.path in page_routes:
                self.path = page_routes[parsed.path]
            return super().do_GET()

        requested_recording_id: str | None = None
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/api/recordings":
                subject = self._single_query_value(query, "subject")
                session = self._single_query_value(query, "session")
                return self._json(
                    {
                        "ok": True,
                        "default_recording": _recording_id(
                            self.default_data_file,
                            self.data_dir,
                        ),
                        "recordings": list_recordings(
                            self.data_dir,
                            self.default_data_file,
                            subject=subject or None,
                            session=session or None,
                        ),
                    }
                )

            requested_recording_id = self._single_query_value(
                query,
                RECORDING_QUERY_PARAMETER,
            )
            data_file, recording_id = resolve_recording(
                self.data_dir,
                self.default_data_file,
                requested_recording_id,
            )
            if parsed.path == "/api/health":
                return self._json(
                    {
                        "ok": data_file.is_file(),
                        "service": "fnirs-dashboard",
                        "engine": f"Cedalion {cedalion.__version__}",
                        "data_file": data_file.name,
                        "recording": {
                            "id": recording_id,
                            "filename": data_file.name,
                        },
                    }
                )

            if parsed.path == "/api/settings":
                config, source = self._current_config()
                return self._json(analysis_config_payload(config, source))

            analysis = self._load_selected_analysis(data_file, recording_id)
            if parsed.path == "/api/analysis-metadata":
                return self._json({"ok": True, **analysis_metadata_payload(analysis)})
            if parsed.path == "/api/analysis-metadata-export":
                body, filename = analysis_metadata_bytes(analysis)
                return self._bytes(body, "application/json; charset=utf-8", filename)
            if parsed.path == "/api/probe":
                return self._json(
                    {
                        "ok": True,
                        "recording": analysis.summary["recording"],
                        "analysis_id": analysis.summary["analysis"]["id"],
                        "filename": analysis.summary["filename"],
                        "geometry": analysis.summary["input_validation"]["geometry"],
                        "short_separation": analysis.summary["short_separation"],
                    }
                )
            if parsed.path == "/api/auxiliary":
                return self._json(
                    {
                        "ok": True,
                        "recording": analysis.summary["recording"],
                        "analysis_id": analysis.summary["analysis"]["id"],
                        "inventory": analysis.summary["input_validation"]["auxiliary"],
                        "nuisance_regression": {
                            "auxiliary": analysis.summary["nuisance_regression"]["auxiliary"],
                            "global": analysis.summary["nuisance_regression"]["global"],
                        },
                    }
                )
            if parsed.path == "/api/recording":
                return self._json(
                    {
                        "ok": True,
                        "summary": analysis.summary,
                        "channels": analysis.channels,
                        "series_options": analysis.series_options,
                        "event_counts": analysis.event_counts,
                        "events": analysis.events,
                        "intervals": analysis.intervals,
                        "motion": analysis.motion_summary,
                        "motion_segments": analysis.motion_segments,
                        "quality_summary": analysis.quality_summary,
                        "manual_quality_control": analysis.summary["manual_quality_control"],
                        "task": analysis.task_summary,
                    }
                )
            if parsed.path == "/api/quality":
                return self._json(
                    {
                        "ok": True,
                        "recording": analysis.summary["recording"],
                        "analysis_id": analysis.summary["analysis"]["id"],
                        "summary": analysis.quality_summary,
                        "channels": analysis.quality,
                        "motion": analysis.motion_summary,
                        "motion_segments": analysis.motion_segments,
                        "manual_quality_control": analysis.summary["manual_quality_control"],
                    }
                )
            if parsed.path == "/api/signal":
                kind = query.get("kind", ["conc_filtered"])[0]
                channel_index = int(query.get("channel", ["0"])[0])
                component = query.get("component", [None])[0]
                max_points = int(query.get("max_points", ["1800"])[0])
                return self._json(
                    {
                        "ok": True,
                        "recording": analysis.summary["recording"],
                        "analysis_id": analysis.summary["analysis"]["id"],
                        **signal_payload(
                            analysis,
                            kind,
                            channel_index,
                            component,
                            max_points,
                        ),
                    }
                )
            if parsed.path == "/api/task-response":
                condition = query.get("condition", [None])[0]
                channel_index = int(query.get("channel", ["0"])[0])
                max_points = int(query.get("max_points", ["600"])[0])
                return self._json(
                    {
                        "ok": True,
                        "recording": analysis.summary["recording"],
                        "analysis_id": analysis.summary["analysis"]["id"],
                        **task_response_payload(
                            analysis,
                            condition,
                            channel_index,
                            max_points,
                        ),
                    }
                )
            if parsed.path == "/api/task-export":
                condition = query.get("condition", [None])[0]
                channel_index = int(query.get("channel", ["0"])[0])
                body, filename = task_csv_bytes(analysis, condition, channel_index)
                return self._bytes(body, "text/csv; charset=utf-8", filename)
            if parsed.path == "/api/task-glm":
                condition = query.get("condition", [None])[0]
                channel_index = int(query.get("channel", ["0"])[0])
                contrast = query.get("contrast", [None])[0]
                return self._json(
                    {
                        "ok": True,
                        "recording": analysis.summary["recording"],
                        "analysis_id": analysis.summary["analysis"]["id"],
                        **task_glm_payload(
                            analysis,
                            condition,
                            channel_index,
                            contrast,
                        ),
                    }
                )
            if parsed.path == "/api/task-glm-export":
                body, filename = task_glm_csv_bytes(analysis)
                return self._bytes(body, "text/csv; charset=utf-8", filename)

            self._error_json(HTTPStatus.NOT_FOUND, "接口不存在")
        except FileNotFoundError:
            label = requested_recording_id or self.default_data_file.name
            self._error_json(HTTPStatus.NOT_FOUND, f"找不到记录文件：{label}")
        except (IndexError, ValueError) as exc:
            self._error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self.log_error("Cedalion analysis error: %s", exc)
            self._error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Cedalion 分析失败：{exc}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            try:
                payload = self._request_json()
                with ANALYSIS_CONFIG_LOCK:
                    config = self.analysis_config_store.save(payload)
                    type(self).analysis_config = config
                    type(self).analysis_config_source = "网页设置"
                self._json(analysis_config_payload(config, "网页设置"))
            except (IndexError, ValueError) as exc:
                self._error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                self.log_error("Settings update error: %s", exc)
                self._error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"保存分析设置失败：{exc}")
            return
        if parsed.path == "/api/settings/reset":
            try:
                with ANALYSIS_CONFIG_LOCK:
                    type(self).analysis_config = self.analysis_config_store.reset()
                    type(self).analysis_config_source = "服务器环境变量"
                    config = type(self).analysis_config
                self._json(analysis_config_payload(config, "服务器环境变量"))
            except Exception as exc:
                self.log_error("Settings reset error: %s", exc)
                self._error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"恢复默认分析设置失败：{exc}")
            return
        if parsed.path != "/api/qc-decisions":
            self._error_json(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
            requested_recording_id = self._single_query_value(
                query, RECORDING_QUERY_PARAMETER
            )
            data_file, recording_id = resolve_recording(
                self.data_dir, self.default_data_file, requested_recording_id
            )
            payload = self._request_json()
            labels = payload.get("bad_channel_labels")
            if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
                raise ValueError("bad_channel_labels 必须是字符串数组")
            current = self._load_selected_analysis(data_file, recording_id)
            available = {channel["label"] for channel in current.channels}
            unknown = sorted(set(labels) - available)
            if unknown:
                raise ValueError("不存在的通道：" + "、".join(unknown))
            decisions = self.qc_decision_store.update(recording_id, labels)
            analysis = load_analysis(
                data_file, self.analysis_config, recording_id, decisions
            )
            self._json(
                {
                    "ok": True,
                    "recording": analysis.summary["recording"],
                    "analysis_id": analysis.summary["analysis"]["id"],
                    "manual_quality_control": analysis.summary["manual_quality_control"],
                    "summary": analysis.quality_summary,
                    "channels": analysis.quality,
                }
            )
        except FileNotFoundError:
            self._error_json(HTTPStatus.NOT_FOUND, "找不到记录文件")
        except (IndexError, ValueError) as exc:
            self._error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except OSError as exc:
            self.log_error("QC decision storage error: %s", exc)
            self._error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"无法保存人工质量决定：{exc}")
        except Exception as exc:
            self.log_error("QC decision error: %s", exc)
            self._error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"质量决定更新失败：{exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the Cedalion fNIRS dashboard")
    parser.add_argument("--host", default=os.getenv("FNIRS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("FNIRS_PORT", "8080")))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        analysis_config = AnalysisConfig.from_environment()
    except ValueError as exc:
        raise SystemExit(f"FNIRS 分析参数错误：{exc}") from exc
    data_dir = Path(os.getenv("FNIRS_DATA_DIR", DEFAULT_DATA_DIR)).expanduser().resolve()
    filename = os.getenv("FNIRS_DEFAULT_FILE", DEFAULT_FILENAME)
    if not filename or "\x00" in filename or Path(filename).is_absolute():
        raise SystemExit("FNIRS_DEFAULT_FILE 必须是 FNIRS_DATA_DIR 内的相对路径")
    data_file = (data_dir / filename).resolve()
    try:
        _recording_id(data_file, data_dir)
    except ValueError as exc:
        raise SystemExit("FNIRS_DEFAULT_FILE 必须是 FNIRS_DATA_DIR 内的文件") from exc
    if data_file.suffix.lower() != SNIRF_SUFFIX:
        raise SystemExit("FNIRS_DEFAULT_FILE 必须指向 .snirf 文件")
    if data_file == data_dir:
        raise SystemExit("FNIRS_DEFAULT_FILE 必须是 FNIRS_DATA_DIR 内的文件")

    DashboardHandler.data_dir = data_dir
    DashboardHandler.default_data_file = data_file
    DashboardHandler.environment_config = analysis_config
    settings_filename = Path(
        os.getenv("FNIRS_ANALYSIS_SETTINGS_FILE", data_dir / ".fnirs-dashboard-settings.json")
    ).expanduser().resolve()
    DashboardHandler.analysis_config_store = AnalysisConfigStore(
        settings_filename, analysis_config
    )
    try:
        active_config, config_source = DashboardHandler.analysis_config_store.load()
    except ValueError as exc:
        raise SystemExit(f"FNIRS 分析设置错误：{exc}") from exc
    DashboardHandler.analysis_config = active_config
    DashboardHandler.analysis_config_source = config_source
    qc_filename = Path(
        os.getenv("FNIRS_QC_DECISIONS_FILE", data_dir / ".fnirs-dashboard-qc.json")
    ).expanduser()
    DashboardHandler.qc_decision_store = QcDecisionStore(qc_filename.resolve())
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Cedalion dashboard: http://{args.host}:{args.port}")
    print(f"SNIRF data directory: {data_dir}")
    print(f"Default SNIRF data file: {data_file}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
