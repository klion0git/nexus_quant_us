"""Path bootstrap ensuring submodule or sibling nexus_quant_core and nexus_quant_us root are importable."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
SUBMODULE_CORE_ROOT: Path = PROJECT_ROOT / "libs" / "nexus_quant_core"
SIBLING_CORE_ROOT: Path = PROJECT_ROOT.parent / "nexus_quant_core"
NEXUS_QUANT_CORE_ROOT: Path = (
    SUBMODULE_CORE_ROOT
    if (SUBMODULE_CORE_ROOT / "nexus_quant_core").is_dir()
    else SIBLING_CORE_ROOT
)


def ensure_nexus_quant_core_on_path() -> None:
    """Insert nexus_quant_core (libs/ submodule first, sibling fallback) and nexus_quant_us root into sys.path."""
    for candidate in (NEXUS_QUANT_CORE_ROOT, PROJECT_ROOT):
        path_str = str(candidate)
        if candidate.is_dir() and path_str not in sys.path:
            sys.path.insert(0, path_str)


ensure_nexus_quant_core_on_path()
