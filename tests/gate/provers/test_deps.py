"""Tests for `gate.provers.deps` and lean4's dependency-probe hooks.

Probe-builder and parser tests against canned output, plus the shared
runner's two refusals — a prover with no probe, and a probe that exits
nonzero — both of which must raise rather than report an empty project.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest

from gate.provers import ProverError
from gate.provers.base import DeclDependency
from gate.provers.deps import collect_dependencies
from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import (
    LEAN4,
    build_lean_dependency_probe,
    parse_lean_dependencies,
)


def _line(*fields: str) -> str:
    return "\t".join(("choir-dep", *fields))


# ---------------------------------------------------------------------------
# lean4: probe builder
# ---------------------------------------------------------------------------


def test_probe_imports_every_module_and_reports_on_it(tmp_path: Path) -> None:
    probe = build_lean_dependency_probe(tmp_path, ["Proj", "Proj.Core"])
    assert probe == tmp_path / ".choir-deps-probe.lean"
    text = probe.read_text()
    assert text.startswith("import Lean\nimport Proj\nimport Proj.Core\n")
    assert "[`Proj, `Proj.Core]" in text


def test_probe_reads_proofs_through_the_axiom_traversal(tmp_path: Path) -> None:
    """`value?` is `None` for an imported theorem; reading it reports nothing."""
    text = build_lean_dependency_probe(tmp_path, ["Proj"]).read_text()
    assert "getUsedConstantsAsSet" in text
    assert "value?" not in text


def test_probe_writes_a_watched_name_as_itself(tmp_path: Path) -> None:
    """A subscript survives as a character, not as a `\\u` escape lean may
    spell differently."""
    text = build_lean_dependency_probe(
        tmp_path, ["Proj"], ["MeasureTheory.mul_meas_ge_le_lintegral\u2080"]
    ).read_text()
    assert '"MeasureTheory.mul_meas_ge_le_lintegral\u2080"' in text
    assert "\\u2080" not in text


def test_collect_hands_the_watch_list_to_the_probe(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def command(ws: Path, imports: list[str], watched: list[str]) -> list[str]:
        seen.append(watched)
        return ["printf", ""]

    profile = dataclasses.replace(LEAN4, dependency_command=command)
    collect_dependencies(profile, tmp_path, ["Proj"], ["Real.binEntropy"])
    assert seen == [["Real.binEntropy"]]


def test_collect_writes_nothing_into_the_project(tmp_path: Path) -> None:
    """The reading must not be able to change what it reads, even if the
    run dies partway."""
    line = _line("theorem", "Proj", "1", "complete", "Proj.foo", "", "")
    profile = dataclasses.replace(
        LEAN4, dependency_command=lambda ws, imports, watched: ["printf", "%s\\n", line]
    )
    collect_dependencies(profile, tmp_path, ["Proj"])
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# lean4: parser
# ---------------------------------------------------------------------------


def test_parse_splits_statement_and_proof_dependencies() -> None:
    output = _line("theorem", "Proj.Core", "12", "complete", "Proj.foo", "Proj.w", "Proj.a,Proj.b")
    assert parse_lean_dependencies(output) == [
        DeclDependency(
            decl="Proj.foo",
            kind="theorem",
            module="Proj.Core",
            line=12,
            has_placeholder=False,
            uses=("Proj.w",),
            proof_uses=("Proj.a", "Proj.b"),
        )
    ]


def test_parse_reads_a_placeholder_off_the_term() -> None:
    output = _line("theorem", "Proj.Core", "3", "placeholder", "Proj.open", "", "")
    (entry,) = parse_lean_dependencies(output)
    assert entry.has_placeholder
    assert entry.uses == () and entry.proof_uses == ()


def test_parse_ignores_build_noise() -> None:
    output = "\n".join(
        [
            "warning: unused variable",
            "",
            "choir-dep\tmalformed",
            _line("definition", "Proj", "1", "complete", "Proj.w", "", ""),
            _line("theorem", "Proj", "x", "complete", "Proj.bad", "", ""),
        ]
    )
    assert [e.decl for e in parse_lean_dependencies(output)] == ["Proj.w"]


# ---------------------------------------------------------------------------
# the shared runner
# ---------------------------------------------------------------------------


def test_collect_refuses_a_prover_with_no_probe(tmp_path: Path) -> None:
    with pytest.raises(ProverError, match="no dependency probe"):
        collect_dependencies(ISABELLE, tmp_path, ["Proj"])


def test_collect_returns_nothing_for_no_modules(tmp_path: Path) -> None:
    assert collect_dependencies(LEAN4, tmp_path, []) == []


def test_collect_raises_and_leaves_nothing_behind_when_the_probe_fails(
    tmp_path: Path,
) -> None:
    profile = dataclasses.replace(
        LEAN4, dependency_command=lambda ws, imports, watched: _failing_command(ws)
    )
    with pytest.raises(ProverError, match="dependency probe failed"):
        collect_dependencies(profile, tmp_path, ["Proj"])
    assert list(tmp_path.iterdir()) == []


def test_collect_parses_a_successful_run(tmp_path: Path) -> None:
    line = _line("theorem", "Proj", "7", "complete", "Proj.foo", "", "Proj.bar")
    profile = dataclasses.replace(
        LEAN4, dependency_command=lambda ws, imports, watched: ["printf", "%s\\n", line]
    )
    (entry,) = collect_dependencies(profile, tmp_path, ["Proj"])
    assert entry.decl == "Proj.foo"
    assert entry.proof_uses == ("Proj.bar",)


def _failing_command(workspace: Path) -> list[str]:
    (workspace / ".choir-deps-probe.lean").write_text("import Nope\n")
    return ["false"]


# ---------------------------------------------------------------------------
# live toolchain
#
# The probe's three load-bearing facts about lean are facts about a lean
# version — that an imported theorem's proof is reachable only through
# `getUsedConstantsAsSet`, that a `private` name arrives mangled, and that
# a generated declaration has no source range. Nothing but a real run
# catches the day one of them stops holding.
# ---------------------------------------------------------------------------

_LAKE_AND_LEAN = bool(shutil.which("lake") and shutil.which("lean"))

_SOURCE = """\
def w : Nat := 1

private theorem helper : w = 1 := rfl

theorem main : w = 1 := helper

theorem open_goal : w = 2 := sorry

def two_args (a b : Nat) : Nat :=
  if b = 0 then a else two_args b (a % b)
termination_by b
decreasing_by sorry

mutual
theorem step_a : (n : Nat) → w = 2
  | 0 => open_goal
  | n + 1 => step_b n
termination_by n => n
theorem step_b : (n : Nat) → w = 2
  | 0 => open_goal
  | n + 1 => step_a n
termination_by n => n
end
"""


def _toolchain() -> str:
    pinned = Path(__file__).resolve().parents[3] / "samples" / "lean4" / "lean-toolchain"
    return pinned.read_text().strip() if pinned.exists() else "leanprover/lean4:stable"


@pytest.mark.slow
@pytest.mark.skipif(
    not _LAKE_AND_LEAN,
    reason="lake/lean not on PATH — the live dependency probe needs a local elan toolchain",
)
def test_live_lean4_dependency_probe(tmp_path: Path) -> None:
    project = tmp_path / "dep_probe_project"
    project.mkdir()
    (project / "lean-toolchain").write_text(_toolchain() + "\n")
    (project / "lakefile.toml").write_text(
        'name = "DepProbe"\n'
        'defaultTargets = ["DepProbe"]\n'
        "\n"
        "[[lean_lib]]\n"
        'name = "DepProbe"\n'
    )
    (project / "DepProbe.lean").write_text(_SOURCE)

    build = subprocess.run(
        ["lake", "build"], cwd=project, capture_output=True, text=True,
        timeout=600, check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    found = {entry.decl: entry for entry in collect_dependencies(LEAN4, project, ["DepProbe"])}

    # Exactly what the source declares: a generated congruence or equation
    # lemma would show up here, and must not.
    assert set(found) == {
        "w", "helper", "main", "open_goal", "two_args", "step_a", "step_b",
    }

    # A private helper arrives unmangled, at both ends of its edge.
    assert found["main"].proof_uses == ("helper",)
    assert found["helper"].decl == "helper"

    # The statement/proof split is the one the plan records.
    assert found["main"].uses == ("w",)
    assert found["helper"].uses == ("w",)

    # A library declaration is reported only once the watch list names it,
    # and then only that one: `w`'s type reaches `Nat`, and its value
    # reaches `OfNat.ofNat` and `instOfNatNat` too, which stay out.
    assert found["w"].uses == ()
    watched = {
        entry.decl: entry
        for entry in collect_dependencies(LEAN4, project, ["DepProbe"], ["Nat"])
    }
    assert watched["w"].uses == ("Nat",)
    assert set(watched) == set(found)
    assert all(
        "OfNat.ofNat" not in entry.uses + entry.proof_uses
        for entry in watched.values()
    )

    assert found["open_goal"].has_placeholder is True
    assert found["main"].has_placeholder is False

    # Lean compiles recursion on several arguments and mutual recursion
    # through auxiliaries the source never names (`two_args._unary`,
    # `step_a._mutual`). What sits behind one belongs to the declaration
    # that was written: a placeholder there is its placeholder, and a lemma
    # used there is one it uses.
    assert found["two_args"].has_placeholder is True
    assert "open_goal" in found["step_a"].proof_uses
    assert "open_goal" in found["step_b"].proof_uses
    assert found["step_a"].has_placeholder is False
    assert found["main"].module == "DepProbe"
    assert found["w"].kind == "definition" and found["main"].kind == "theorem"
