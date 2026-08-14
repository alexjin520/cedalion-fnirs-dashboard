---
name: cedalion-fnirs-dashboard
description: Maintain and extend the Cedalion-based fNIRS dashboard in this project with small, readable, research-aware changes. Use when changing fnirs-dashboard/server.py, its API, SNIRF processing pipeline, quality control, task analysis, exports, or deployment scripts.
---

# Cedalion fNIRS Dashboard

## Overview

Keep the dashboard easy to read while making its analysis results more trustworthy and reproducible. Prefer a small, explicit implementation that follows the existing Python standard-library HTTP server and Cedalion/xarray pipeline.

## Working Rules

- Read `fnirs-dashboard/README.md`, the target file, and nearby frontend code before editing.
- Preserve the current request flow and data model unless the requested feature cannot fit it.
- Use plain functions, dataclasses, and descriptive names. Add an abstraction only when it removes real duplication.
- Keep processing steps visible in `server.py`; do not hide scientific decisions behind a generic pipeline framework.
- Keep user-facing errors actionable and return consistent JSON from `/api/*`.
- Keep defaults conservative. Make scientific parameters configurable before adding many new algorithms.
- Do not silently change existing results. Record changed parameters, excluded channels, and fallback behavior in response metadata.
- Use ASCII for new code unless existing Chinese UI or documentation makes localized text necessary.

## SNIRF Format Authority

Treat the upstream [SNIRF file specification](https://github.com/fNIRS/snirf/blob/master/snirf_specification.md) as the canonical format authority for SNIRF I/O changes. Check its HDF5 type, required-field, and indexing rules before making parser or writer assumptions.

In particular, `metaDataTags/SubjectID` is a required generic string; do not impose an ASCII-only restriction that the specification does not state. SNIRF string datasets must use null-terminated, variable-length `H5T_C_S1` storage. If an analysis-only compatibility copy is needed for a limited third-party reader, preserve this string representation in the copy and never alter the source SNIRF.

## Current Analysis Contract

Treat this as the existing contract unless the user asks to change it:

`SNIRF -> valid positive amplitudes -> optical density -> HbO/HbR -> frequency filtering`

Quality currently uses SNR, SCI, PSP diagnostics, and GVTD candidates. Descriptive task analysis uses TDDR, epochs, baseline correction, condition averages, SEM, and response-window metrics. The task GLM uses a Gamma HRF and cosine drift regressors; its default OLS path can add the closest quality-passing short channel for each long channel. Explicit auxiliary regressors must be selected by name, pass recorded time-unit/coverage/gap/variance checks, use anti-aliasing before downsampling, and be z-scored after resampling. Global regression is never default and currently uses Cedalion's self-inclusive modeled-channel mean. AR-IRLS keeps unfiltered TDDR concentration for prewhitening. Wavelet output is an experimental continuous-signal comparison, not the default task result.

Records are selected lazily with the `recording` query parameter, constrained to a relative `.snirf` path inside `FNIRS_DATA_DIR`. Keep the bounded per-file cache request-local: never mutate a handler-global selected file in `ThreadingHTTPServer`. Each successful cached analysis has a reproducibility manifest with its input SHA-256, stable analysis ID, timestamp, software versions, parameters, validation, exclusion reasons, and GLM state; task and GLM exports must retain that identity.

When changing this contract, update the README and the frontend labels together.

## Feature Priority

The following foundations are already implemented: configurable parameters, pre-analysis input/geometry validation, short-channel identification, a per-channel task GLM with condition contrasts, explicit auxiliary/global GLM regression, lazy multi-file selection, a bounded per-file analysis cache, reproducibility manifests, persistent manual bad-channel decisions, and optional GVTD task-epoch exclusion. Preserve their metadata and explicit fallback states. GVTD `exclude_epochs` currently applies only to task averages; GLM must explicitly retain its regular-time-axis fallback until censored fitting is validated.

Implement remaining work in this order when the user has not specified otherwise:

1. Add ROI support only when the project has an explicit optode-to-ROI mapping; do not infer ROIs from source/detector names.
2. Add bounded asynchronous batch jobs only when concrete batch volumes and status requirements are known; do not make `/api/recordings` eagerly process every file.
3. Harden deployment with bounded requests, authentication, reverse proxy, structured logs, and a real readiness check.

Do not start with a broad rewrite, a database, or a complex job framework for this file-backed dashboard.

## Change Workflow

1. Inspect the existing implementation and identify the smallest ownership boundary for the change.
2. State the scientific assumption in a short code comment or metadata field when it is not obvious.
3. Implement the simplest compatible path. Reuse Cedalion functions already imported by the project.
4. Exercise the affected helper and API with the sample SNIRF files when available.
5. Run syntax checks and focused tests; report unavailable dependencies or tests instead of hiding them.
6. Update README/API descriptions when behavior, parameters, or deployment changes.
7. Keep `fnirs-dashboard/FEATURES.md` accurate in the same change whenever a user-visible feature, its page/location, navigation, interaction, API, export, or displayed analysis result changes. A purely internal refactor with no such behavior change does not need a documentation edit.

## Validation Checklist

- Load at least one valid sample through `load_analysis`.
- Check missing stimuli, missing geometry, one wavelength, zero-filled channels, and no passing channels.
- Verify `allow_nan=False` JSON responses contain `null`, not NaN or Infinity.
- Verify invalid query parameters produce 400 responses and missing files produce 404 responses.
- Verify `/api/recordings` only inventories allowed `.snirf` files, a selected `recording` is carried through signal/task/quality/export APIs, and path traversal or duplicate selection returns 400.
- Verify `/api/analysis-metadata` and its download contain a SHA-256, stable analysis ID, timestamp, direct runtime dependency versions, full parameters, preprocessing exclusions, quality exclusion reasons, and GLM settings. Check CSV exports include the same analysis ID and input hash.
- Verify `/api/auxiliary` reports the inventory, explicit selected names, resampling/anti-aliasing audit, rejection reasons, and actual auxiliary/global GLM state. A nonzero auxiliary `time_offset` must never be silently applied.
- Check that exported CSV/PNG labels match the selected condition and channel.
- For a valid multi-condition sample, verify `/api/task-glm` contains HbO/HbR beta, CI, t, p, and q values, condition contrast results, and a JSON-safe unavailable state when fitting cannot proceed.
- Verify `/api/task-glm-export` contains all modeled-channel condition and contrast rows, and `/api/probe` reports the geometry summary and channel classification.
- Confirm no unrelated files or generated runtime logs are included in the change.
