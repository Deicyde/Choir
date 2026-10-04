"""Derive a project's roadmap graph from its built source.

`reconcile` is the pure fold of a dependency probe into a graph;
`sync_graph` runs the probe and writes the file. For a plan kept another
way there is no graph to write: `compare` is the pure check of that
plan's claims against the probe, `check_plan` runs it, and
`declarations` hands over the probe's reading as it stands.
"""

from orchestrator.graph.claims import PlanCheck, PlanClaimsError, compare, parse_claims
from orchestrator.graph.probe import check_plan, declarations
from orchestrator.graph.reconcile import Reconciliation, reconcile
from orchestrator.graph.sync import (
    GraphSyncError,
    derive,
    project_modules,
    sync_graph,
)

__all__ = [
    "GraphSyncError",
    "PlanCheck",
    "PlanClaimsError",
    "Reconciliation",
    "check_plan",
    "compare",
    "declarations",
    "derive",
    "parse_claims",
    "project_modules",
    "reconcile",
    "sync_graph",
]
