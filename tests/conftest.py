"""pytest configuration shared across all tests/ subpackages.

Adds the ``tests_resources`` directory to ``sys.path`` so that helper
modules (e.g. ``api_json_helper``) used by Robot Framework tests can
also be imported by pytest unit tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PROJECT_ROOT / "tests_resources"

if str(RESOURCES_DIR) not in sys.path:
    sys.path.insert(0, str(RESOURCES_DIR))
