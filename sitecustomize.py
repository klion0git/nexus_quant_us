"""Auto-bootstrap submodule or sibling nexus_quant_core package for out-of-the-box imports in nexus_quant_us."""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_SUBMODULE_CORE_DIR = _PROJECT_ROOT / "libs" / "nexus_quant_core"
_SIBLING_CORE_DIR = _PROJECT_ROOT.parent / "nexus_quant_core"
_NEXUS_QUANT_CORE_DIR = (
    _SUBMODULE_CORE_DIR
    if (_SUBMODULE_CORE_DIR / "nexus_quant_core").is_dir()
    else _SIBLING_CORE_DIR
)

for _candidate in (_NEXUS_QUANT_CORE_DIR, _PROJECT_ROOT):
    _path_str = str(_candidate)
    if _candidate.is_dir() and _path_str not in sys.path:
        sys.path.insert(0, _path_str)
