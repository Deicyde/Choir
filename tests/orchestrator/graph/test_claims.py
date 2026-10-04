"""Tests for `orchestrator.graph.claims`.

The comparison is the only thing standing between an authored plan and
the build, so each way the two can drift apart has to be caught, and an
honest plan has to pass clean — a check that cries wolf gets turned off.
"""

from __future__ import annotations

from typing import Any

import pytest

from gate.provers.base import DeclDependency
from orchestrator.graph.claims import PlanClaimsError, PlanNode, compare, parse_claims


def dep(decl: str, *, uses: tuple[str, ...] = (), proof_uses: tuple[str, ...] = (),
        placeholder: bool = False) -> DeclDependency:
    return DeclDependency(
        decl=decl, kind="theorem", module="Proj.Core", line=1,
        has_placeholder=placeholder, uses=uses, proof_uses=proof_uses,
    )


def node(node_id: str, *decls: str, stated: bool = True, proved: bool = False,
         uses: tuple[str, ...] = (), proof_uses: tuple[str, ...] = ()) -> PlanNode:
    return PlanNode(id=node_id, decls=decls, stated=stated, proved=proved,
                    uses=uses, proof_uses=proof_uses)


def found(nodes: list[PlanNode], deps: list[DeclDependency]) -> list[tuple[str, str | None, str]]:
    return [(f.code, f.node, f.decl) for f in compare(nodes, deps).findings]


def test_an_honest_plan_agrees_with_the_build() -> None:
    """Open work recorded as open is not drift."""
    result = compare(
        [
            node("def", "Proj.f", proved=True),
            node("done", "Proj.done", proved=True, uses=("def",), proof_uses=("open",)),
            node("open", "Proj.open", uses=("def",), proof_uses=("done",)),
        ],
        [
            dep("Proj.f"),
            dep("Proj.done", uses=("Proj.f",), proof_uses=("Proj.open",)),
            dep("Proj.open", uses=("Proj.f",), placeholder=True),
        ],
    )
    assert result.agrees and result.findings == () and result.helpers == ()


def test_a_proof_claimed_over_a_placeholder_is_reported() -> None:
    assert found(
        [node("main", "Proj.main", proved=True)],
        [dep("Proj.main", placeholder=True)],
    ) == [("proof-claimed-over-placeholder", "main", "Proj.main")]


def test_a_proof_that_landed_without_the_plan_recording_it_is_reported() -> None:
    assert found([node("main", "Proj.main")], [dep("Proj.main")]) == [
        ("proof-not-recorded", "main", "Proj.main")
    ]


def test_a_node_is_unproved_until_every_declaration_it_names_is() -> None:
    """One finished declaration out of two does not finish the node."""
    assert found(
        [node("main", "Proj.a", "Proj.b")],
        [dep("Proj.a"), dep("Proj.b", placeholder=True)],
    ) == []
    assert found([node("main", "Proj.a", "Proj.b")], [dep("Proj.a")]) == [
        ("declaration-missing", "main", "Proj.b")
    ]


def test_a_stated_node_with_no_declaration_claims_nothing_checkable() -> None:
    assert found([node("prose-only")], []) == []


def test_a_declaration_the_plan_still_calls_unstated_is_reported() -> None:
    assert found(
        [node("main", "Proj.main", stated=False)],
        [dep("Proj.main", placeholder=True)],
    ) == [("statement-not-recorded", "main", "Proj.main")]


def test_an_unstated_node_whose_declaration_does_not_exist_is_just_planned() -> None:
    assert found([node("later", "Proj.later", stated=False)], []) == []


def test_a_placeholder_no_node_names_is_reported_and_a_finished_helper_is_listed() -> None:
    result = compare(
        [node("main", "Proj.main", proved=True)],
        [dep("Proj.main"), dep("Proj.gap", placeholder=True), dep("Proj.helper")],
    )
    assert [(f.code, f.node, f.decl) for f in result.findings] == [
        ("unplanned-placeholder", None, "Proj.gap")
    ]
    assert result.helpers == ("Proj.helper",)


def test_a_placeholder_is_seen_when_two_declarations_share_its_name() -> None:
    """Lean reports a private declaration under its user-facing name, so
    a finished public lemma can share one with an open private one in
    another module. Whichever record came last used to stand for both."""
    for main in ([dep("Proj.main", placeholder=True), dep("Proj.main")],
                 [dep("Proj.main"), dep("Proj.main", placeholder=True)]):
        result = compare(
            [node("main", "Proj.main", proved=True)],
            [*main, dep("Proj.aux", placeholder=True), dep("Proj.aux")],
        )
        assert [(f.code, f.node, f.decl) for f in result.findings] == [
            ("proof-claimed-over-placeholder", "main", "Proj.main"),
            ("unplanned-placeholder", None, "Proj.aux"),
        ]
        assert result.helpers == ()


def test_two_nodes_naming_one_declaration_is_reported_once() -> None:
    assert found(
        [node("first", "Proj.main", proved=True), node("second", "Proj.main", proved=True)],
        [dep("Proj.main")],
    ) == [("duplicate-target", "second", "Proj.main")]


def test_a_proof_dependency_the_plan_has_no_edge_for_is_reported() -> None:
    deps = [dep("Proj.main", proof_uses=("Proj.lemma",)), dep("Proj.lemma", placeholder=True)]
    lemma = node("lemma", "Proj.lemma")
    assert found([node("main", "Proj.main", proved=True), lemma], deps) == [
        ("edge-missing", "main", "Proj.main")
    ]
    for recorded in ({"uses": ("lemma",)}, {"proof_uses": ("lemma",)}):
        assert found([node("main", "Proj.main", proved=True, **recorded), lemma], deps) == []


def test_a_statement_dependency_must_be_recorded_as_one() -> None:
    """Filed under `proof_uses`, the plan says the node can be stated
    before the definition it is stated against."""
    deps = [dep("Proj.main", uses=("Proj.f",)), dep("Proj.f")]
    f = node("f", "Proj.f", proved=True)
    assert found([node("main", "Proj.main", proved=True, proof_uses=("f",)), f], deps) == [
        ("edge-missing", "main", "Proj.main")
    ]
    assert found([node("main", "Proj.main", proved=True, uses=("f",)), f], deps) == []


def test_a_dependency_the_plan_reaches_along_a_path_is_not_owed_an_edge() -> None:
    """A finding has to mean the plan computes something false. Along a
    recorded path it computes the same thing as along a direct edge."""
    deps = [
        dep("Proj.main", proof_uses=("Proj.mid", "Proj.lemma")),
        dep("Proj.mid", proof_uses=("Proj.lemma",)),
        dep("Proj.lemma", placeholder=True),
    ]
    main = node("main", "Proj.main", proved=True, proof_uses=("mid",))
    lemma = node("lemma", "Proj.lemma")
    assert found([main, node("mid", "Proj.mid", proved=True, proof_uses=("lemma",)), lemma],
                 deps) == []
    assert found([main, node("mid", "Proj.mid", proved=True), lemma], deps) == [
        ("edge-missing", "main", "Proj.main"),
        ("edge-missing", "mid", "Proj.mid"),
    ]


def test_a_statement_is_not_reached_by_way_of_a_proof_edge() -> None:
    deps = [dep("Proj.main", uses=("Proj.g", "Proj.f")), dep("Proj.g"), dep("Proj.f")]
    main = node("main", "Proj.main", proved=True, uses=("g",))
    f = node("f", "Proj.f", proved=True)
    assert found([main, node("g", "Proj.g", proved=True, proof_uses=("f",)), f], deps) == [
        ("edge-missing", "main", "Proj.main")
    ]
    assert found([main, node("g", "Proj.g", proved=True, uses=("f",)), f], deps) == []


def test_a_helper_does_not_hide_the_dependency_behind_it() -> None:
    result = compare(
        [node("main", "Proj.main", proved=True), node("open", "Proj.open")],
        [
            dep("Proj.main", proof_uses=("Proj.step",)),
            dep("Proj.step", proof_uses=("Proj.inner",)),
            dep("Proj.inner", proof_uses=("Proj.open",)),
            dep("Proj.open", placeholder=True),
        ],
    )
    [finding] = result.findings
    assert (finding.code, finding.node, finding.decl) == ("edge-missing", "main", "Proj.main")
    assert "Proj.step" in finding.message and "Proj.inner" in finding.message
    assert result.helpers == ("Proj.inner", "Proj.step")


def test_the_walk_stops_at_the_first_planned_declaration() -> None:
    """What a planned lemma rests on is that lemma's edge to record."""
    assert found(
        [
            node("main", "Proj.main", proved=True, proof_uses=("mid",)),
            node("mid", "Proj.mid", proved=True, proof_uses=("base",)),
            node("base", "Proj.base", proved=True),
        ],
        [
            dep("Proj.main", proof_uses=("Proj.mid",)),
            dep("Proj.mid", proof_uses=("Proj.base",)),
            dep("Proj.base"),
        ],
    ) == []


def test_a_placeholder_is_held_to_its_statement_only() -> None:
    """There is no proof term yet, so `proof_uses` is still the plan's to
    choose; the statement's type is there either way."""
    nodes = [
        node("main", "Proj.main"),
        node("f", "Proj.f", proved=True),
        node("lemma", "Proj.lemma", proved=True),
    ]
    deps = [
        dep("Proj.main", uses=("Proj.f",), proof_uses=("Proj.lemma",), placeholder=True),
        dep("Proj.f"),
        dep("Proj.lemma"),
    ]
    [finding] = compare(nodes, deps).findings
    assert (finding.code, finding.node) == ("edge-missing", "main")
    assert "Proj.f" in finding.message and "Proj.lemma" not in finding.message


def test_declarations_of_one_node_owe_each_other_no_edge() -> None:
    assert found(
        [node("pair", "Proj.a", "Proj.b", proved=True)],
        [dep("Proj.a", proof_uses=("Proj.b",)), dep("Proj.b")],
    ) == []


def test_an_edge_the_build_does_not_show_is_the_plans_to_keep() -> None:
    """A plan may record a route no term takes yet, or point at a node
    it did not emit."""
    assert found(
        [
            node("main", "Proj.main", proved=True, uses=("elsewhere",), proof_uses=("other",)),
            node("other", "Proj.other", proved=True),
        ],
        [dep("Proj.main"), dep("Proj.other")],
    ) == []


def test_helpers_that_use_each_other_do_not_trap_the_walk() -> None:
    assert found(
        [node("main", "Proj.main", proved=True)],
        [
            dep("Proj.main", proof_uses=("Proj.even",)),
            dep("Proj.even", proof_uses=("Proj.odd",)),
            dep("Proj.odd", proof_uses=("Proj.even", "Proj.main")),
        ],
    ) == []


# ------------------------------------------------------- reading the claims
def _claims(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "id": "main", "decls": ["Proj.main"], "stated": True, "proved": False,
        "uses": ["f"], "proof_uses": [],
    }
    spec.update(overrides)
    return {"nodes": [spec]}


def test_claims_parse_and_extra_keys_are_ignored() -> None:
    [parsed] = parse_claims(_claims(title="Main theorem", decls=["Proj.main", "Proj.main"]))
    assert parsed == node("main", "Proj.main", uses=("f",))


@pytest.mark.parametrize(
    ("document", "names"),
    [
        ([], "nodes"),
        ({"nodes": {}}, "nodes"),
        ({"nodes": ["main"]}, r"nodes\[0\]"),
        (_claims(id=""), "id"),
        (_claims(stated="yes"), "stated"),
        (_claims(proved=1), "proved"),
        (_claims(decls="Proj.main"), "decls"),
        (_claims(uses=[7]), "uses"),
        ({"nodes": [{k: v for k, v in _claims()["nodes"][0].items() if k != "proof_uses"}]},
         "proof_uses"),
        ({"nodes": _claims()["nodes"] * 2}, "main"),
    ],
)
def test_a_malformed_document_is_refused_and_says_where(document: Any, names: str) -> None:
    """A key left out, or misspelt into one that is ignored, must not
    read as a node that claims nothing."""
    with pytest.raises(PlanClaimsError, match=names):
        parse_claims(document)
