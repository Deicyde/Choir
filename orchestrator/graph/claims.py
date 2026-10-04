"""Compare what a plan claims with what the build shows. Pure.

The default plan needs no such comparison: `reconcile` derives its
statuses and edges from the build, so they cannot disagree with it for
longer than one pass. A plan kept another way is authored — somebody
writes "proved" and somebody writes the dependency links — and an
authored claim drifts the moment a merge lands without the edit that
should have gone with it. Nothing fails when that happens. The plan
simply goes on saying something the build no longer does, and every
choice of what to publish next is made from it.

`compare` takes the plan's claims and the probe's `DeclDependency`
records and returns every place they disagree. It decides nothing about
which side is wrong: a proof recorded over a placeholder may be a plan
edited early or a proof that was reverted, and only the overseer knows
which. It reports, the way `reconcile` reports `disputed`, and writes
nothing.

**The claims arrive in a neutral shape** so that the planner — whatever
it is — does the translating, once, at its own edge::

    {"nodes": [{"id": "...", "decls": ["..."], "stated": true,
                "proved": false, "uses": ["..."], "proof_uses": ["..."]}]}

`stated` and `proved` are what the planner says the plan claims, not
something derived here from the plan's files: planners record a proof in
different ways, and one keeps no proof status for a definition at all.
The build is read one way for all of them, though. A declaration a node
names that holds no placeholder is proved, a definition as much as a
theorem, so a planner whose own records say otherwise translates when it
emits the claims.

**Helpers are permitted.** A declaration no node names and that holds no
placeholder is a lemma someone needed on the way, and Choir lets a
contributor add one. It is listed, not reported. What a helper may not
do is hide a dependency: the plan has to reach every *planned*
declaration a term reaches, directly or through declarations no node
names, or routing a proof through an unplanned lemma would be enough to
make a chapter resting on an open result read as resting on nothing.

**The plan has to reach a dependency, not list it.** An authored plan
records the dependencies its author thought worth naming, and a theorem
that uses a definition's projection has not thereby been given a new
prerequisite if the plan already gets there through the definition. What
a plan computes from its edges — what is ready, what is finished all the
way down — is the same along a recorded path as along a direct edge, so
a path is what is asked for. A finding therefore always means the plan
can compute something false, never that it could have been more
explicit.

**A declaration holding a placeholder has no proof term**, so only its
statement's dependencies are read — the rule `reconcile` applies when it
derives edges, for the same reason. What the plan lists under
`proof_uses` for it is the route someone intends to take, and there is
nothing in the build yet to hold that to.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from gate.provers.base import DeclDependency

_LISTS = ("decls", "uses", "proof_uses")
_FLAGS = ("stated", "proved")


class PlanClaimsError(ValueError):
    """The plan-claims document is not in the shape `parse_claims` reads."""


@dataclass(frozen=True)
class PlanNode:
    """One node of a plan, as the plan itself describes it."""

    id: str
    decls: tuple[str, ...]
    stated: bool
    proved: bool
    uses: tuple[str, ...]
    proof_uses: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    """One disagreement between the plan and the build.

    `node` is `None` only where no node is involved: a placeholder the
    plan has never heard of.
    """

    code: str
    node: str | None
    decl: str
    message: str


@dataclass(frozen=True)
class PlanCheck:
    """Every disagreement, and the declarations the plan leaves unnamed."""

    findings: tuple[Finding, ...]
    helpers: tuple[str, ...]

    @property
    def agrees(self) -> bool:
        return not self.findings

    def summary(self) -> dict[str, Any]:
        return {
            "agrees": self.agrees,
            "findings": [
                {"code": f.code, "node": f.node, "decl": f.decl, "message": f.message}
                for f in self.findings
            ],
            "helpers": list(self.helpers),
        }


def parse_claims(document: Any) -> tuple[PlanNode, ...]:
    """Read a parsed plan-claims document into nodes, or say what is wrong.

    Every one of the six keys is required. Unknown keys are ignored, so
    that a planner can emit whatever else it likes alongside — which
    means a misspelt key is an unknown one, and if the real key were
    optional the node would quietly read as claiming nothing. A claim
    left out by accident has to fail here, where it can be seen.
    """
    if not isinstance(document, dict) or not isinstance(document.get("nodes"), list):
        raise PlanClaimsError('plan claims must be an object with a "nodes" list')
    nodes: list[PlanNode] = []
    seen: set[str] = set()
    for index, raw in enumerate(document["nodes"]):
        where = f"nodes[{index}]"
        if not isinstance(raw, dict):
            raise PlanClaimsError(f"{where} is not an object")
        node_id = raw.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise PlanClaimsError(f'{where}: "id" must be a non-empty string')
        where = f"{where} ({node_id})"
        if node_id in seen:
            raise PlanClaimsError(f"{where}: a node with this id appears earlier")
        seen.add(node_id)
        for key in _FLAGS:
            if not isinstance(raw.get(key), bool):
                raise PlanClaimsError(f'{where}: "{key}" must be true or false')
        for key in _LISTS:
            value = raw.get(key)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                raise PlanClaimsError(f'{where}: "{key}" must be a list of names')
        nodes.append(
            PlanNode(
                id=node_id,
                decls=tuple(dict.fromkeys(raw["decls"])),
                stated=raw["stated"],
                proved=raw["proved"],
                uses=tuple(raw["uses"]),
                proof_uses=tuple(raw["proof_uses"]),
            )
        )
    return tuple(nodes)


def _by_name(deps: Iterable[DeclDependency]) -> dict[str, DeclDependency]:
    """One record per name, folding together the records that share one.

    A probe can report two declarations under one name: Lean's gives a
    private declaration its user-facing name, so a private lemma in one
    module and a public one of the same name in another come back alike.
    A plan names declarations by that name and cannot say which it
    means. Keeping only one record would let the other's placeholder go
    unseen, so the name is held to both: it holds a placeholder if
    either does, and it uses what either uses.
    """
    merged: dict[str, DeclDependency] = {}
    for dep in deps:
        seen = merged.get(dep.decl)
        if seen is None:
            merged[dep.decl] = dep
            continue
        uses = tuple(dict.fromkeys(seen.uses + dep.uses))
        merged[dep.decl] = replace(
            seen,
            has_placeholder=seen.has_placeholder or dep.has_placeholder,
            uses=uses,
            proof_uses=tuple(
                name for name in dict.fromkeys(seen.proof_uses + dep.proof_uses)
                if name not in uses
            ),
        )
    return merged


def _reach(
    start: Iterable[str],
    by_decl: Mapping[str, DeclDependency],
    owners: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    """The planned declarations a term reaches, each with the way there.

    Stops at the first planned declaration on every path — what lies
    beyond it is that node's edge to record, not this one's — and walks
    on through anything unplanned. The value is the chain of unplanned
    declarations passed through, empty for a direct mention, so the
    finding can name the helper the dependency was hiding behind.
    Breadth-first, so the chain reported is a shortest one.
    """
    found: dict[str, tuple[str, ...]] = {}
    seen: set[str] = set()
    queue = deque((decl, ()) for decl in start)
    while queue:
        decl, via = queue.popleft()
        if decl in seen:
            continue
        seen.add(decl)
        if decl in owners:
            found[decl] = via
            continue
        dep = by_decl.get(decl)
        if dep is None:
            continue
        onward = dep.uses if dep.has_placeholder else dep.uses + dep.proof_uses
        queue.extend((target, (*via, decl)) for target in onward)
    return found


def _plan_reach(start: str, edges: Mapping[str, Sequence[str]]) -> frozenset[str]:
    """Every node the plan gets to from `start` along `edges`."""
    seen: set[str] = set()
    queue = deque(edges.get(start, ()))
    while queue:
        node_id = queue.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)
        queue.extend(edges.get(node_id, ()))
    return frozenset(seen)


def _edge_findings(
    node: PlanNode,
    by_decl: Mapping[str, DeclDependency],
    owners: Mapping[str, Sequence[str]],
    statement_edges: Mapping[str, Sequence[str]],
    all_edges: Mapping[str, Sequence[str]],
) -> list[Finding]:
    """Dependencies the build shows from `node` and the plan does not reach.

    A statement dependency has to be reached through `uses` alone: a
    plan that gets there only by way of a `proof_uses` edge is saying
    the node can be stated before the other one is, and it cannot. A
    proof dependency is satisfied along either kind of edge, since a
    node already needed for a statement is needed. One finding per pair
    of nodes, the statement's if both parts reach it.
    """
    findings: list[Finding] = []
    reported: set[str] = set()
    present = [by_decl[decl] for decl in node.decls if decl in by_decl]
    for part, recorded in (
        ("statement", _plan_reach(node.id, statement_edges)),
        ("proof", _plan_reach(node.id, all_edges)),
    ):
        for dep in present:
            if part == "proof" and dep.has_placeholder:
                continue
            start = dep.uses if part == "statement" else dep.proof_uses
            for target, via in sorted(_reach(start, by_decl, owners).items()):
                holders = owners[target]
                other = holders[0]
                if node.id in holders or other in reported:
                    continue
                if recorded.intersection(holders):
                    continue
                reported.add(other)
                through = f" through {', '.join(via)}" if via else ""
                missing = (
                    f"{node.id} does not reach {other} through uses"
                    if part == "statement"
                    else f"{node.id} records no path to {other}"
                )
                findings.append(Finding(
                    "edge-missing", node.id, dep.decl,
                    f"{other} names {target}, which the {part} of {dep.decl} "
                    f"uses{through}, and {missing}",
                ))
    return findings


def compare(nodes: Sequence[PlanNode], deps: Sequence[DeclDependency]) -> PlanCheck:
    """Return every disagreement between `nodes` and the build's `deps`.

    A node's status is held to the declarations it names, one finding
    per declaration that contradicts it, except `proof-not-recorded`,
    which is a claim about the node as a whole: the proof is unrecorded
    only when *every* declaration it names is in the build and none
    holds a placeholder. It is reported once, against the node's first
    declaration.

    Dependencies are checked for every node with a declaration in the
    build, whatever it claims — a plan records what a node will need
    before the node is stated. An edge the plan records and the build
    does not show is not a finding, for the reason `reconcile` keeps hand-written
    edges: a plan may record a dependency no term has yet. Nor is a
    listed id that is not among `nodes`; a plan may point at a node it
    did not emit, a library result for one.
    """
    by_decl = _by_name(deps)
    owners: dict[str, list[str]] = {}
    for node in nodes:
        for decl in node.decls:
            owners.setdefault(decl, []).append(node.id)
    statement_edges = {node.id: node.uses for node in nodes}
    all_edges = {node.id: (*node.uses, *node.proof_uses) for node in nodes}

    findings: list[Finding] = []
    for node in nodes:
        present = [by_decl[decl] for decl in node.decls if decl in by_decl]
        for decl in node.decls:
            first = owners[decl][0]
            if first != node.id:
                findings.append(Finding(
                    "duplicate-target", node.id, decl,
                    f"{decl} is named by {first} and again by {node.id}",
                ))
            dep = by_decl.get(decl)
            if dep is None:
                if node.stated:
                    findings.append(Finding(
                        "declaration-missing", node.id, decl,
                        f"{node.id} claims a statement, but {decl} is not in the build",
                    ))
                continue
            if not node.stated:
                findings.append(Finding(
                    "statement-not-recorded", node.id, decl,
                    f"{node.id} does not claim a statement, but {decl} is in the build",
                ))
            if node.proved and dep.has_placeholder:
                findings.append(Finding(
                    "proof-claimed-over-placeholder", node.id, decl,
                    f"{node.id} claims a proof, but {decl} still holds a placeholder",
                ))
        if (
            node.stated
            and not node.proved
            and node.decls
            and len(present) == len(node.decls)
            and not any(dep.has_placeholder for dep in present)
        ):
            findings.append(Finding(
                "proof-not-recorded", node.id, node.decls[0],
                f"{node.id} does not claim a proof, "
                "but no declaration it names holds a placeholder",
            ))
        findings.extend(
            _edge_findings(node, by_decl, owners, statement_edges, all_edges)
        )

    helpers: list[str] = []
    for dep in by_decl.values():
        if dep.decl in owners:
            continue
        if dep.has_placeholder:
            findings.append(Finding(
                "unplanned-placeholder", None, dep.decl,
                f"{dep.decl} holds a placeholder and no node names it",
            ))
        else:
            helpers.append(dep.decl)

    return PlanCheck(
        findings=tuple(sorted(
            findings, key=lambda f: (f.node is None, f.node or "", f.code, f.decl, f.message)
        )),
        helpers=tuple(sorted(helpers)),
    )
