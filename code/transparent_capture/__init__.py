"""Transparent-depth acquisition pipeline: offline extensions over the
visuotactile capture stack (`code/visuotactile`).

Modules:
- qc: per-frame and per-session quality gates for depth capture.
- calibration: validation of recorded camera calibration/judge fingerprints.
- loader: offline (no-device) loader for recorded sessions.
- synthetic: deterministic synthetic session generator for offline verification.

Evidence classification follows docs/control rules: synthetic results are
never presented as real-device calibration or capture results.
"""
from . import calibration, loader, qc, synthetic  # noqa: F401
from . import dataset_builder, session_recorder  # noqa: F401
