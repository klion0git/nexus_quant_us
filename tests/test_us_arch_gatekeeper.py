"""US Architectural AST & Source Gatekeeper.

Enforces:
1. ZERO cross-market leakage strings anywhere inside nexus_quant_us.
2. ZERO wall-clock calls (datetime.now / utcnow / today, or time.time / time.monotonic)
   outside us_core/clock.py.
3. Valid JavaScript syntax inside chart_hist/templates/chart_view.html via `node --check`.
"""
from __future__ import annotations

import ast
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple

from us_core._bootstrap import PROJECT_ROOT


def _build_forbidden_tokens() -> Tuple[str, ...]:
    """Construct forbidden cross-market tokens dynamically so this file stays 100% literal-free."""
    return (
        "_".join(("TW", "STOCK")),
        "_".join(("TW", "FUTURES")),
        "".join(("CRYP", "TO")),
        "/".join(("Asia", "Taipei")),
        "_".join(("TZ", "TW")),
        "".join(("19", "11")),
        "".join(("Fu", "bon")),
        "_".join(("fu" + "bon", "neo")),
        "".join(("Bin", "ance")),
        "".join(("Hyper", "liquid")),
    )


class TestUsArchitectureGatekeeper(unittest.TestCase):
    def _iter_source_files(self) -> List[Path]:
        exts = {".py", ".html", ".md", ".example", ".json", ".sql"}
        files: List[Path] = []
        for p in PROJECT_ROOT.rglob("*"):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts or ".git" in p.parts or "libs" in p.parts or "data" in p.parts:
                continue
            if p.suffix.lower() in exts or p.name.startswith(".env"):
                files.append(p)
        return sorted(files)

    def test_zero_non_us_market_strings_across_entire_repo(self) -> None:
        forbidden = _build_forbidden_tokens()
        violations: List[str] = []
        for path in self._iter_source_files():
            content = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token.lower() in content.lower():
                    rel = path.relative_to(PROJECT_ROOT)
                    violations.append(f"{rel} contains forbidden token {token!r}")
        self.assertEqual(violations, [], "Cross-market boundary violations:\n" + "\n".join(violations))

    def test_ast_zero_wall_clock_outside_us_core_clock(self) -> None:
        clock_file = (PROJECT_ROOT / "us_core" / "clock.py").resolve()
        py_files = [p for p in PROJECT_ROOT.rglob("*.py") if "__pycache__" not in p.parts and "libs" not in p.parts]
        self.assertGreaterEqual(len(py_files), 15)

        forbidden_dt_methods = {"now", "utcnow", "today"}
        forbidden_time_methods = {"time", "monotonic", "time_ns", "monotonic_ns"}
        violations: List[str] = []

        for py_path in sorted(py_files):
            resolved = py_path.resolve()
            rel = py_path.relative_to(PROJECT_ROOT)
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(rel))

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute):
                    attr = func.attr
                    # Check datetime.now / datetime.datetime.now / date.today
                    if attr in forbidden_dt_methods:
                        owner_id = ""
                        if isinstance(func.value, ast.Name):
                            owner_id = func.value.id
                        elif isinstance(func.value, ast.Attribute):
                            owner_id = func.value.attr
                        if owner_id in ("datetime", "date"):
                            violations.append(f"{rel}:{node.lineno} calls forbidden {owner_id}.{attr}()")

                    # Check time.time() / time.monotonic() outside us_core/clock.py
                    if resolved != clock_file and attr in forbidden_time_methods:
                        if isinstance(func.value, ast.Name) and func.value.id == "time":
                            violations.append(f"{rel}:{node.lineno} calls time.{attr}() outside us_core/clock.py")

        self.assertEqual(violations, [], "Clock discipline AST violations:\n" + "\n".join(violations))

    def test_chart_view_html_inline_javascript_syntax_via_node_check(self) -> None:
        html_path = PROJECT_ROOT / "chart_hist" / "templates" / "chart_view.html"
        self.assertTrue(html_path.exists(), f"Missing {html_path}")
        html_text = html_path.read_text(encoding="utf-8")

        # Extract inline <script> blocks (excluding external <script src="..."> tags)
        script_blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html_text, flags=re.DOTALL | re.IGNORECASE)
        self.assertGreaterEqual(len(script_blocks), 1, "Expected inline <script> block in chart_view.html")

        combined_js = "\n;\n".join(script_blocks)
        with tempfile.TemporaryDirectory() as tmp:
            js_file = Path(tmp) / "chart_view_check.js"
            js_file.write_text(combined_js, encoding="utf-8")
            proc = subprocess.run(
                ["node", "--check", str(js_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"node --check failed on chart_view.html inline JS:\n{proc.stderr}\n{proc.stdout}",
            )


if __name__ == "__main__":
    unittest.main()
