"""Ask a built checkout about its own declarations, with no graph involved.

The I/O half of `orchestrator.graph.claims`, and the probe half of
`sync.derive` on its own: find the project's modules, require the
checkout built at its current source, and run the prover's dependency
probe. Nothing here reads or writes `roadmap/graph.json`, because the
project this is for does not have one — its plan is kept another way,
and what it needs from Choir is the build's side of the comparison.

`declarations` hands that side over as data, for a planner that wants to
do its own reading. `check_plan` does the comparison itself, against the
plan's claims in the neutral shape `claims.parse_claims` documents. It
is to a plan kept another way what `sync_graph(check=True)` is to the
default one, with one difference that follows from who owns the plan:
there is no write mode, since Choir does not know how that plan is
stored and has no business editing it.

Both refuse a stale checkout for the reason `sync.require_built` gives.
It matters more here, if anything: a finding read off last week's
artifacts says the plan is wrong when it is the build that is behind.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from gate.provers import get_profile
from gate.provers.base import DeclDependency
from gate.provers.deps import collect_dependencies
from gate.provers.select import read_prover
from orchestrator.graph.claims import PlanClaimsError, PlanNode, compare, parse_claims
from orchestrator.graph.sync import GraphSyncError, project_modules, require_built


def probe(
    checkout: Path, *, prover: str | None = None
) -> tuple[dict[str, str], list[DeclDependency]]:
    """The project's modules, and what the prover says each declares.

    No watch list is passed, so the records name the project's own
    declarations only. A library result a plan cites is not something
    this build proves, and the planner leaves such nodes out of its
    claims for the same reason.
    """
    profile = get_profile(prover or read_prover(checkout))
    modules = project_modules(checkout, profile)
    if not modules:
        raise GraphSyncError(f"no {profile.name} source found under {checkout}")
    require_built(checkout, profile)
    return modules, collect_dependencies(profile, checkout, sorted(modules))


def declarations(checkout: Path, *, prover: str | None = None) -> list[dict[str, Any]]:
    """One record per declaration of the project, as the build has it.

    `file` is the repo-relative source path rather than the probe's
    module name: a path is what a plan and a task both point at, and
    only this side knows where a module's source sits.
    """
    modules, deps = probe(checkout, prover=prover)
    return [
        {
            "decl": dep.decl,
            "file": modules.get(dep.module),
            "kind": dep.kind,
            "has_placeholder": dep.has_placeholder,
            "uses": list(dep.uses),
            "proof_uses": list(dep.proof_uses),
        }
        for dep in sorted(deps, key=lambda d: d.decl)
    ]


def read_claims(source: str) -> tuple[PlanNode, ...]:
    """Read plan claims from a file, or from stdin when `source` is `-`.

    Stdin is the expected route: the claims are a planner's rendering of
    its own files, made for this one question, and a pipe leaves no
    second copy of the plan lying in the checkout to go stale.

    `ValueError` and not only the JSON error: a file that is not UTF-8
    fails in the decoding, before any JSON is parsed, and is as much the
    caller's to fix.
    """
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, ValueError) as exc:
        origin = "stdin" if source == "-" else source
        raise PlanClaimsError(f"unreadable plan claims from {origin}: {exc}") from exc
    return parse_claims(document)


def check_plan(checkout: Path, plan: str, *, prover: str | None = None) -> dict[str, Any]:
    """Report where the plan's claims and the built checkout disagree.

    The claims are read first. A malformed document is the caller's to
    fix and costs nothing to find, whereas the probe imports the whole
    project.
    """
    nodes = read_claims(plan)
    _, deps = probe(checkout, prover=prover)
    return compare(nodes, deps).summary()
