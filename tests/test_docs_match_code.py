"""The docs must not describe behaviour the engine does not have.

The Run 8 wiring audit found six functions with tests but no caller in `src/`,
three of which the docs described in the present tense: partial pooling was
"reported" by nothing, combined scenarios were "modeled jointly" by nothing,
and a negotiation gap "informed" a start price that was never implemented.
Nothing failed, because no test connected prose to call sites.

This closes that gap mechanically. Every function the prose docs name either
has a call site in `src/` and a test that exercises it, or is listed below as
deliberately unwired AND marked as such in the docs a reader sees. The
allowlist is also checked from the other side: an entry that has since gained
a caller is a stale exemption and fails, so the docs get corrected when the
code catches up.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
TESTS = REPO / "tests"

# Docs written in the active voice about what the engine does. The build log
# is deliberately excluded: it is a dated record of what each phase did, not a
# description of current behaviour.
PROSE_DOCS = [
    REPO / "README.md",
    REPO / "docs" / "methodology.md",
    REPO / "docs" / "equations.md",
    REPO / "docs" / "metric-dictionary.md",
]

# Functions the docs name that have NO caller in src/ on purpose. Each entry
# is the reason, and each must be disclosed in the docs by a phrase from
# NOT_WIRED_MARKERS appearing in the same document that names it.
#
# Adding to this list is a decision to keep tested-but-unreachable code. It
# needs a reason that survives being read aloud: "the alternative is worse",
# not "it might be useful later".
DELIBERATELY_UNWIRED = {
    "optimize_targets": (
        "The demand optimizer. README states twice that no run produces a "
        "computed optimal price; wiring it in would falsify that, deleting it "
        "would discard tested logic the README points at."
    ),
    "optimal_price_win_aware": (
        "The win-aware optimizer. Same bargain as optimize_targets, and it is "
        "where the refusal to multiply a win curve by a demand elasticity is "
        "written down and pinned by test."
    ),
    "run_cost_scenario": (
        "The four-row pass-through strategy table. Superseded by the 1% grid, "
        "which contains the canonical 0/50/100% points; kept because the "
        "grid's values are pinned against it."
    ),
    "fetch_tariff_rates": (
        "USITC HTS rate fetch. Phase 29.3 asked for the source to be "
        "evaluated and recorded; wiring it would need an HTS-to-category map "
        "that does not exist and a baseline to subtract from a published level."
    ),
}

# KNOWN LIMIT of the reachability check below: a call from inside another dead
# function counts as a call, so this test sees "reachable from somewhere" and
# not "reachable from an entry point". parse_general_rate is the live example -
# only fetch_tariff_rates calls it, and nothing calls that. Whole-island dead
# code is vulture's job (see .vulture_allowlist.py); this test's job is the
# narrower and more dangerous case, where prose claims behaviour.

NOT_WIRED_MARKERS = ("not wired in", "not implemented", "not yet verified live")


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.fixture(scope="module")
def code() -> tuple[dict[str, Path], set[str], set[str]]:
    """Functions defined in src/, names called in src/, names used in tests/."""
    defined: dict[str, Path] = {}
    called: set[str] = set()
    for path in _py_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                defined.setdefault(node.name, path)
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
            elif isinstance(node, ast.ImportFrom):
                # A re-export is a use: the name reaches callers through it.
                called.update(a.name for a in node.names)
    in_tests: set[str] = set()
    for path in _py_files(TESTS):
        text = path.read_text(encoding="utf-8")
        in_tests.update(re.findall(r"\b([a-z_][a-z0-9_]{3,})\b", text))
    return defined, called, in_tests


def _documented_functions(defined: dict[str, Path]) -> dict[str, list[Path]]:
    """Backticked identifiers in the prose docs that name a src/ function."""
    found: dict[str, list[Path]] = {}
    for doc in PROSE_DOCS:
        text = doc.read_text(encoding="utf-8")
        for name in set(re.findall(r"`([a-z_][a-z0-9_]{3,})`", text)):
            if name in defined:
                found.setdefault(name, []).append(doc)
    return found


def test_every_documented_function_is_reachable_or_declared_unwired(
    code: tuple[dict[str, Path], set[str], set[str]],
) -> None:
    defined, called, _ = code
    documented = _documented_functions(defined)
    assert documented, "no documented functions found - the doc scan is broken"

    undeclared = sorted(
        name
        for name in documented
        if name not in called and name not in DELIBERATELY_UNWIRED
    )
    assert not undeclared, (
        "the docs describe these functions, but nothing in src/ calls them:\n  "
        + "\n  ".join(f"{n} ({defined[n].relative_to(REPO)})" for n in undeclared)
        + "\n\nEither wire it into the pipeline, or correct the docs and add it "
        "to DELIBERATELY_UNWIRED with the reason."
    )


def test_unwired_functions_say_so_where_a_reader_will_see_it(
    code: tuple[dict[str, Path], set[str], set[str]],
) -> None:
    """A reason in a test file is not a disclosure. The document that names an
    unwired function has to admit it, in words."""
    defined, _called, _ = code
    documented = _documented_functions(defined)
    missing: list[str] = []
    for name, docs in documented.items():
        if name not in DELIBERATELY_UNWIRED:
            continue
        for doc in docs:
            text = doc.read_text(encoding="utf-8").lower()
            if not any(marker in text for marker in NOT_WIRED_MARKERS):
                missing.append(f"{name} named in {doc.relative_to(REPO)}")
    assert not missing, (
        "these documents name a function that no run reaches, without saying so:\n  "
        + "\n  ".join(missing)
        + f"\n\nAdd one of {NOT_WIRED_MARKERS} to the passage."
    )


def test_the_unwired_allowlist_has_no_stale_entries(
    code: tuple[dict[str, Path], set[str], set[str]],
) -> None:
    """The other direction. A function that has gained a caller is no longer an
    exemption, and the docs that call it unwired are now the wrong ones."""
    defined, called, _ = code
    stale = sorted(n for n in DELIBERATELY_UNWIRED if n in called)
    assert not stale, (
        "these are listed as deliberately unwired but src/ now calls them:\n  "
        + "\n  ".join(stale)
        + "\n\nRemove the entry and the 'not wired in' wording in the docs."
    )
    unknown = sorted(n for n in DELIBERATELY_UNWIRED if n not in defined)
    assert not unknown, (
        "these are listed as deliberately unwired but no longer exist:\n  "
        + "\n  ".join(unknown)
    )


def test_documented_and_reachable_features_are_exercised_by_a_test(
    code: tuple[dict[str, Path], set[str], set[str]],
) -> None:
    """The second half of the rule: a feature the docs describe as active must
    have a call site AND a test that touches it. Code nothing tests is not
    evidence, whatever the prose says."""
    defined, called, in_tests = code
    documented = _documented_functions(defined)
    untested = sorted(
        name
        for name in documented
        if name in called and name not in DELIBERATELY_UNWIRED and name not in in_tests
    )
    assert not untested, (
        "the docs describe these as active and src/ calls them, but no test "
        "names them:\n  " + "\n  ".join(untested)
    )
