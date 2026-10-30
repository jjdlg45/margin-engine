"""Guards on the validation study itself.

A GitHub run of `scripts/measure_validation.py` spent about two hours getting
twenty checks right and then died in the twenty-first on a call left behind by
a signature change: `fit_win_curves(qi, seg_map)` after the function had grown
a third argument. Nothing was written, so the twenty good measurements went
with it. Three things came out of that, and this file holds two of them:

* **Signatures.** Every call this script makes to an engine function is bound
  against that function's real signature here, in milliseconds. A missing
  argument fails the pull request instead of the overnight study.
* **Names.** Every row the script can produce must have an entry in
  `docs/validation-meta.yaml`, or the generated documents refuse to render at
  the end of a run that has already cost two hours.

The third is `--smoke`, which runs every check on tiny worlds in CI: binding a
signature cannot see a function whose RETURN shape changed (the same call also
unpacked a `DataFrame` that had become a tuple), and only running the code
catches that.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from margin_engine.validation_table import load_meta

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "measure_validation.py"
META = REPO / "docs" / "validation-meta.yaml"

#: Stand-in for an argument whose value is unknowable without running the
#: call. `Signature.bind` only matches parameters to arguments, so the value
#: never matters.
PLACEHOLDER = object()


@pytest.fixture(scope="module")
def script() -> ModuleType:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("measure_validation", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Registered before execution: the module defines dataclasses, and
    # `dataclasses` resolves their annotations through sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _checkable_calls(mod: ModuleType) -> list[tuple[ast.Call, Any, str]]:
    """Every call in the script to a function we can resolve and bind.

    Only plain `name(...)` calls are checked, and only when `name` resolves to
    a Python function in the module's own namespace. Calls that splat
    (`f(*args)` or `f(**kw)`) are skipped: their parameter list is not knowable
    from the source.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        obj = getattr(mod, node.func.id, None)
        if obj is None or not inspect.isfunction(obj):
            continue
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue
        out.append((node, obj, node.func.id))
    return out


def test_every_call_matches_the_function_it_calls(script: ModuleType) -> None:
    calls = _checkable_calls(script)
    assert len(calls) > 20, "the call scanner found almost nothing, so it is not scanning"
    problems = []
    for node, obj, name in calls:
        try:
            inspect.signature(obj).bind(
                *[PLACEHOLDER] * len(node.args),
                **{str(kw.arg): PLACEHOLDER for kw in node.keywords},
            )
        except TypeError as exc:
            problems.append(f"line {node.lineno}: {name}(...) - {exc}")
    assert not problems, "calls that do not match their function:\n" + "\n".join(problems)


def test_every_declared_row_has_a_scope_entry(script: ModuleType) -> None:
    """A row with no entry in validation-meta.yaml makes the generated
    documents refuse to render - which, before this test, happened only after
    the study had already run."""
    meta = load_meta(META)
    missing = [name for name in script.declared_checks() if name not in meta]
    assert not missing, "these rows have no entry in docs/validation-meta.yaml:\n" + "\n".join(
        f"  {m}" for m in missing
    )


def test_declared_rows_are_unique(script: ModuleType) -> None:
    names = list(script.declared_checks())
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"two checks declare the same row name: {duplicates}"


def test_the_scope_document_describes_nothing_nobody_measures(script: ModuleType) -> None:
    """The other direction: an entry for a row no script produces describes a
    result that does not exist. Rows measured by the other measurement scripts
    are carried in `docs/validation.json`, so they count as produced."""
    import json

    meta = load_meta(META)
    measured = {
        str(r["check"])
        for r in json.loads((REPO / "docs" / "validation.json").read_text(encoding="utf-8"))[
            "results"
        ]
    }
    orphans = sorted(set(meta) - set(script.declared_checks()) - measured)
    assert not orphans, f"scope entries nothing produces: {orphans}"


def test_the_smoke_scale_is_smaller_than_the_full_one(script: ModuleType) -> None:
    """A smoke run that is not actually small is a two-hour pull-request gate."""
    full, smoke = script.FULL, script.SMOKE
    assert smoke.size is not None and full.size is None
    assert len(smoke.coverage_seeds) < len(full.coverage_seeds)
    assert len(smoke.cluster_seeds) < len(full.cluster_seeds)
    assert smoke.seed_cap is not None and full.seed_cap is None
    assert len(smoke.misspecified_seeds) < len(full.misspecified_seeds)
    assert len(smoke.invalid_seeds) < len(full.invalid_seeds)


def test_a_raising_check_becomes_a_failed_row_rather_than_a_lost_run(
    script: ModuleType, tmp_path: Path
) -> None:
    """The actual failure that prompted all this: one check raises, and the
    run still reports - under the row names the documents know."""

    def boom(_tmp: Path) -> list[dict[str, Any]]:
        raise TypeError("fit_win_curves() missing 1 required positional argument: 'rules'")

    check = script.Check(("Win-curve price coefficient",), boom)
    rows = script.run_check(check, tmp_path)
    assert [r["check"] for r in rows] == ["Win-curve price coefficient"]
    assert rows[0]["passed"] is False
    assert "missing 1 required positional argument" in rows[0]["achieved"]
    assert "Traceback" in rows[0]["error"]
