"""Tests for `orchestrator.graph.probe` — the build's side, without a graph."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import orchestrator.graph.probe as probe_mod
from gate.provers.base import DeclDependency
from orchestrator.graph.claims import PlanClaimsError
from orchestrator.graph.sync import GraphSyncError

CLAIMS = {"nodes": [{
    "id": "main", "decls": ["Proj.main"], "stated": True, "proved": True,
    "uses": [], "proof_uses": [],
}]}


def _project(root: Path) -> Path:
    """A checkout with source and no `roadmap/graph.json`."""
    (root / "Proj").mkdir(parents=True)
    (root / "Proj" / "Core.lean").write_text("theorem main : True := sorry\n")
    return root


def _built(monkeypatch: pytest.MonkeyPatch, deps: list[DeclDependency]) -> list[str]:
    order: list[str] = []
    monkeypatch.setattr(probe_mod, "read_prover", lambda c: "lean4")
    monkeypatch.setattr(probe_mod, "require_built", lambda c, p: order.append("check"))
    monkeypatch.setattr(
        probe_mod, "collect_dependencies",
        lambda profile, ws, imports: order.append("probe") or deps,
    )
    return order


def _main(placeholder: bool) -> DeclDependency:
    return DeclDependency(
        decl="Proj.main", kind="theorem", module="Proj.Core", line=1,
        has_placeholder=placeholder, uses=(), proof_uses=("Proj.helper",),
    )


def test_declarations_are_reported_by_source_file_from_a_fresh_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = _built(monkeypatch, [_main(placeholder=True)])
    assert probe_mod.declarations(_project(tmp_path)) == [{
        "decl": "Proj.main", "file": "Proj/Core.lean", "kind": "theorem",
        "has_placeholder": True, "uses": [], "proof_uses": ["Proj.helper"],
    }]
    assert order == ["check", "probe"]


def test_a_checkout_with_no_source_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty probe would read as a project with nothing in it, and
    every stated node would come back missing."""
    _built(monkeypatch, [])
    with pytest.raises(GraphSyncError, match="no lean4 source"):
        probe_mod.declarations(tmp_path)


def test_claims_are_read_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _built(monkeypatch, [_main(placeholder=False)])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(CLAIMS)))
    assert probe_mod.check_plan(_project(tmp_path), "-")["agrees"] is True


@pytest.mark.parametrize("text", ["{not json", json.dumps({"nodes": [{"id": "main"}]})])
def test_bad_claims_are_refused_before_the_project_is_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    order = _built(monkeypatch, [])
    plan = tmp_path / "claims.json"
    plan.write_text(text)
    with pytest.raises(PlanClaimsError):
        probe_mod.check_plan(_project(tmp_path / "checkout"), str(plan))
    assert order == []


def test_a_missing_claims_file_is_reported_as_such(tmp_path: Path) -> None:
    with pytest.raises(PlanClaimsError, match="unreadable plan claims"):
        probe_mod.check_plan(tmp_path, str(tmp_path / "absent.json"))


def test_a_claims_file_that_is_not_utf8_is_reported_as_such(tmp_path: Path) -> None:
    """It fails in the decoding, not the JSON."""
    plan = tmp_path / "claims.json"
    plan.write_bytes(b'\xff\xfe{"nodes": []}')
    with pytest.raises(PlanClaimsError, match="unreadable plan claims"):
        probe_mod.check_plan(tmp_path, str(plan))
