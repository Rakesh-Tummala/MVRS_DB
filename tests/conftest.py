"""Shared test setup.

Keeps every temporary file inside the project, because the C: drive on the
development machine is nearly full.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = PROJECT_ROOT / ".tmp"

# pytest's --basetemp (set in pyproject.toml) lives under .tmp, which is
# git-ignored and so does not exist in a fresh clone.
TMP_ROOT.mkdir(exist_ok=True)
tempfile.tempdir = str(TMP_ROOT)
