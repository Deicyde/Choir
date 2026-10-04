"""Tests for `orchestrator.prs` (mocked gh subprocess)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from gate.checks import REQUIRED_PRESENT
from orchestrator import prs as prs_mod
from orchestrator.prs import (
    PRError,
    get_pr,
    list_open_prs,
    merge_pr,
)

# The unpatched `_gh`, for the one test that needs a `gh` failure to arrive
# the way it really does rather than through the fixtures below.
_REAL_GH = prs_mod._gh


@dataclass
class _FakeProc:
    stdout: str
    stderr: str = ""
    returncode: int = 0


class _BasePolicy:
    """What GitHub serves for `.choir/verify.toml` at the PR's base commit.

    `text=None` is a project with no such file, which is the default so the
    tests about checks need not mention it. `gh_fails` makes the read itself
    fail; `commit_resolves=False` answers as GitHub does for a commit it
    cannot find, where the file field is null for a different reason than
    absence. `file` replaces the file object outright, for the answers that
    are a file but not one whose text can be read.
    """

    def __init__(self) -> None:
        self.text: str | None = None
        self.file: dict | None = None
        self.gh_fails = False
        self.commit_resolves = True
        self.asked: list[tuple[str, ...]] = []

    def answer(self, args: tuple[str, ...]) -> str:
        self.asked.append(args)
        if self.gh_fails:
            raise PRError("gh api graphql failed — HTTP 502")
        file = self.file
        if file is None and self.text is not None:
            file = {"text": self.text, "isTruncated": False}
        commit = {"oid": "a" * 40} if self.commit_resolves else None
        return json.dumps({"data": {"repository": {"commit": commit, "file": file}}})


@pytest.fixture(autouse=True)
def base_policy(monkeypatch) -> _BasePolicy:  # type: ignore[no-untyped-def]
    """Answer the preflight's policy read; every other `gh` call falls
    through to whatever `subprocess.run` the test installed."""
    policy = _BasePolicy()
    real_gh = prs_mod._gh

    def fake_gh(*args: str) -> str:
        if args[:2] == ("api", "graphql"):
            return policy.answer(args)
        return real_gh(*args)

    monkeypatch.setattr(prs_mod, "_gh", fake_gh)
    return policy


@pytest.fixture(autouse=True)
def rest_listing(monkeypatch) -> list[str]:  # type: ignore[no-untyped-def]
    """Answer `get_pr`'s REST file listing: the lines `gh --jq` prints.

    Left empty, it names the paths the test's `gh pr view` payload carried,
    so a test states a PR's files once. A test fills it to serve what
    `gh pr view` cannot show: files past the hundredth, or the old path of
    a rename.
    """
    listing: list[str] = []
    viewed: list[str] = []
    inner_gh = prs_mod._gh

    def fake_gh(*args: str) -> str:
        if "--paginate" in args:
            lines = listing or [f["path"] for f in json.loads(viewed[-1])["files"]]
            return "".join(f"{line}\n" for line in lines)
        out = inner_gh(*args)
        if args[:2] == ("pr", "view"):
            viewed.append(out)
        return out

    monkeypatch.setattr(prs_mod, "_gh", fake_gh)
    return listing


def _run_gh(monkeypatch, payload: dict) -> list[list[str]]:  # type: ignore[no-untyped-def]
    """Serve `payload` as the PR and record every `gh` call made."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _merges(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "merge" in c]


def _pr_payload(**overrides) -> dict:  # type: ignore[no-untyped-def]
    base = {
        "number": 5,
        "title": "prove Sample.foo",
        "url": "https://x/pull/5",
        "author": {"login": "alice"},
        "state": "OPEN",
        "body": "Proof attached.\n\nCloses #3",
        "baseRefOid": "a" * 40,
        "headRefOid": "b" * 40,
        "labels": [{"name": "choir/submission"}],
        "files": [{"path": "Sample/Foo.lean"}],
        "statusCheckRollup": [
            {"name": "verify-pr / rebuild", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "verify-sorry / check", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        "mergeable": "MERGEABLE",
        # Same-repo by default. Spec D4 makes a *fork* head the normal shape
        # in production, but most tests here are about check logic, so the
        # simpler head keeps them focused; the fork case is exercised
        # explicitly below.
        "headRepository": {"name": "proj"},
        "headRepositoryOwner": {"login": "alice"},
    }
    base.update(overrides)
    return base


def _green_rollup(
    *, except_for: str | tuple[str, ...] = (), **overrides: dict
) -> list[dict]:  # type: ignore[no-untyped-def]
    """A `statusCheckRollup` with every `REQUIRED_PRESENT` check green.

    `overrides` maps a check name to a `{"status": ..., "conclusion": ...}`
    dict: a name matching one of the required checks replaces its entry, any
    other name is appended as an extra check. Lets each `merge_pr` test start
    from a fully green, fully-present rollup and flip only the one thing
    under test, so tests aimed at `blocking_failures` don't accidentally also
    trip the separate missing-required-check path.

    `except_for` is shorthand for the common case: one or more required
    check names (still derived from `REQUIRED_PRESENT`, so the fixture
    cannot drift from the registry) marked `COMPLETED`/`FAILURE` instead of
    `SUCCESS`, present-but-red rather than absent — the shape per-prover
    tests need (`statement-equiv` red, everything else green).
    """
    if isinstance(except_for, str):
        except_for = (except_for,)
    base = {
        name: {"status": "COMPLETED", "conclusion": "SUCCESS"}
        for name in REQUIRED_PRESENT
    }
    for name in except_for:
        base[name] = {"status": "COMPLETED", "conclusion": "FAILURE"}
    base.update(overrides)
    return [{"name": name, **fields} for name, fields in base.items()]


def test_status_context_shape_normalized(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # gh mixes CheckRun and StatusContext shapes; the latter uses
    # `context`/`state` instead of `name`/`status`.
    payload = json.dumps(_pr_payload(statusCheckRollup=[
        {"context": "legacy-ci", "state": "SUCCESS"},
        {"context": "legacy-pending", "state": "PENDING"},
    ]))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    pr = get_pr("alice/proj", 5)
    # A terminal commit-status state maps onto the CheckRun split: the API
    # gives one `state` for both. Previously `state` landed in `status`, so a
    # green legacy status was never COMPLETED and blocked every merge forever.
    assert pr.checks[0].name == "legacy-ci"
    assert pr.checks[0].status == "COMPLETED"
    assert pr.checks[0].conclusion == "SUCCESS"
    # Non-terminal states are left alone, so they keep blocking.
    assert pr.checks[1].status == "PENDING"
    assert pr.checks[1].conclusion == ""


def test_merge_pr_default_squash_delete(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(statusCheckRollup=_green_rollup())
    captured = []

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            captured.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("alice/proj", 5)
    cmd = captured[0]
    assert "--squash" in cmd
    assert "--delete-branch" in cmd
    assert "--match-head-commit" in cmd
    assert payload["headRefOid"] in cmd


def test_merge_pr_does_not_delete_a_branch_on_a_contributors_fork(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec D4 puts every ordinary contribution's head branch on a fork the
    orchestrator cannot write to. `gh pr merge --delete-branch` would attempt
    the deletion anyway and can fail *after* the merge lands — turning a
    successful merge into a raised PRError, whose obvious remedy (re-run the
    merge) then hits an already-merged PR."""
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(),
        headRepository={"name": "proj"},
        headRepositoryOwner={"login": "contributor"},
    )
    captured = []

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            captured.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("alice/proj", 5)
    cmd = captured[0]
    assert "--delete-branch" not in cmd
    # The merge itself still happens, and still pins the head SHA.
    assert "--squash" in cmd
    assert payload["headRefOid"] in cmd


def test_merge_pr_branch_protection_rejection_surfaces(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # If the gate's required checks aren't green, GitHub refuses the
    # merge; the orchestrator must see that as an error, not success.
    payload = _pr_payload(statusCheckRollup=_green_rollup())

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            raise subprocess.CalledProcessError(
                1, cmd, stderr="Pull request is not mergeable: required status checks"
            )
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PRError, match="required status checks"):
        merge_pr("alice/proj", 5)


def test_merge_pr_refuses_failing_blocking_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            rebuild={"status": "COMPLETED", "conclusion": "FAILURE"}
        )
    )
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PRError, match="rebuild"):
        merge_pr("o/r", 7)
    assert merged == [], "must not call gh pr merge"


def test_merge_pr_ignores_failing_advisory_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            style={"status": "COMPLETED", "conclusion": "FAILURE"},
            review={"status": "COMPLETED", "conclusion": "FAILURE"},
        )
    )
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("o/r", 7)
    assert len(merged) == 1


def test_merge_pr_allows_green_legacy_status_context(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A green third-party commit status must not wedge the merge.

    `codecov/patch` is an unregistered name, so it is blocking (fail-safe),
    and it arrives in the legacy `context`/`state` shape with no
    `status`/`conclusion`. Before the normalization it read as
    blocking-and-never-COMPLETED: a permanent refusal whose only escape was
    `force`, which is exactly the habit the refusal message must not teach.
    """
    rollup = _green_rollup()
    rollup.append({"context": "codecov/patch", "state": "SUCCESS"})
    payload = _pr_payload(statusCheckRollup=rollup)
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("o/r", 7)
    assert len(merged) == 1


def test_merge_pr_refuses_pending_blocking_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            rebuild={"status": "IN_PROGRESS", "conclusion": ""}
        )
    )
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="rebuild"):
        merge_pr("o/r", 7)


def test_merge_pr_refuses_queued_blocking_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # QUEUED is the state a merge race against a just-triggered rerun
    # actually produces — IN_PROGRESS alone doesn't cover it.
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            rebuild={"status": "QUEUED", "conclusion": ""}
        )
    )
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="rebuild"):
        merge_pr("o/r", 7)


def test_merge_pr_refuses_when_no_checks_ran(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(statusCheckRollup=[])
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="no checks"):
        merge_pr("o/r", 7)


def test_merge_pr_refuses_missing_required_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A PR whose head branch deletes a verify workflow file makes that
    # check simply never trigger for its own PR — it's absent, not
    # failing, and absence must refuse a merge exactly like a failure.
    payload = _pr_payload(
        statusCheckRollup=[
            {"name": "style", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
    )
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PRError, match="missing required checks") as exc_info:
        merge_pr("o/r", 7)
    assert "rebuild" in str(exc_info.value)
    assert "axiom-honesty" in str(exc_info.value)
    assert merged == [], "must not call gh pr merge"


def test_merge_pr_merges_a_lean4_pr_with_only_statement_equiv_red(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """On lean4 comparator is the statement gate, so statement-equiv is advisory."""
    payload = _pr_payload(statusCheckRollup=_green_rollup(except_for="statement-equiv"))
    merged: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "merge" in cmd:
            merged.append(cmd)
            return _FakeProc(stdout="")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("o/r", 7, prover="lean4")
    assert len(merged) == 1


def test_merge_pr_refuses_the_same_pr_on_isabelle(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(statusCheckRollup=_green_rollup(except_for="statement-equiv"))
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="statement-equiv"):
        merge_pr("o/r", 7, prover="isabelle")


def test_merge_pr_without_a_prover_refuses(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Omitting the prover must take the strict path."""
    payload = _pr_payload(statusCheckRollup=_green_rollup(except_for="statement-equiv"))
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: _FakeProc(stdout=json.dumps(payload))
    )
    with pytest.raises(PRError, match="statement-equiv"):
        merge_pr("o/r", 7)


def test_merge_pr_force_rejects_blank_reason() -> None:
    with pytest.raises(PRError, match="non-blank reason string"):
        merge_pr("o/r", 7, force="   ")


def test_merge_pr_force_requires_reason_and_leaves_a_trace(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(
        statusCheckRollup=_green_rollup(
            rebuild={"status": "COMPLETED", "conclusion": "FAILURE"}
        )
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    # The reason deliberately shares no substring with any check name, so
    # the "rebuild" assertion below can only pass if the override comment
    # injects the failing check name itself.
    merge_pr("o/r", 7, force="toolchain bump; verified locally")

    commented = [c for c in calls if any("/comments" in part for part in c)]
    assert commented, "an override must leave a comment on the PR"
    assert any("verified locally" in part for part in commented[0])
    assert any("rebuild" in part for part in commented[0]), (
        "the comment must name what it overrode, not just the given reason"
    )
    assert any("merge" in c for c in calls)
    merge_cmd = next(c for c in calls if "merge" in c)
    assert "--match-head-commit" in merge_cmd and "b" * 40 in merge_cmd, (
        "an override must merge the exact commit its audit trail names"
    )

    comment_idx = next(
        i for i, c in enumerate(calls) if any("/comments" in part for part in c)
    )
    merge_idx = next(i for i, c in enumerate(calls) if "merge" in c)
    assert comment_idx < merge_idx, "the override comment must post before the merge"


def test_merge_pr_force_names_prover_and_non_blocking_red(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 2026-08-20, finding F4 minor 4: on the force path, a check that
    is visibly red but not blocking *for the resolved prover*
    (`statement-equiv` relaxed to advisory on lean4) must not vanish from the
    audit trail behind an unqualified "all required checks were green" —
    the override comment needs to name the prover basis and the check that
    was red under it, since neither `missing` nor `failures` mentions it."""
    payload = _pr_payload(statusCheckRollup=_green_rollup(except_for="statement-equiv"))
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    merge_pr("o/r", 7, force="toolchain bump; verified locally", prover="lean4")

    commented = [c for c in calls if any("/comments" in part for part in c)]
    assert commented, "an override must leave a comment on the PR"
    body = "\n".join(commented[0])
    assert "green" in body  # nothing was blocking under this resolution
    assert "lean4" in body, "the comment must name the prover basis"
    assert "statement-equiv" in body, (
        "a visibly red but non-blocking check must not disappear from the trail"
    )


def test_get_pr_paginates_past_the_hundred_file_cap(monkeypatch, rest_listing) -> None:  # type: ignore[no-untyped-def]
    """`gh pr view --json files` truncates at 100 with no error and no
    marker. `docs/agents/ORCHESTRATOR.md` § Boundaries has the orchestrator refuse
    any PR touching `.github/`, and that rule reads `pr.files` — so a PR
    padded past the cap could push its `.github/` edit out of the list the
    refusal ever examines. That is the same fail-open five gate CLIs were
    fixed to paginate past; the merge path is the one place it still mattered.
    """
    padded = [{"path": f"Sample/Pad{i}.lean"} for i in range(100)]
    payload = _pr_payload(files=padded, statusCheckRollup=_green_rollup())
    rest_listing.extend(
        [f"Sample/Pad{i}.lean" for i in range(100)]
        + [".github/workflows/verify-axiom-honesty.yml"]
    )
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: _FakeProc(stdout=json.dumps(payload))
    )
    pr = get_pr("alice/proj", 5)
    assert ".github/workflows/verify-axiom-honesty.yml" in pr.files
    assert len(pr.files) == 101
    assert pr.files_truncated is False


def test_list_open_prs_flags_a_truncated_file_list_rather_than_hiding_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The survey view stays one cheap request, so it must say when its
    answer is partial — a caller must not read a truncated list as complete."""
    padded = [{"path": f"Sample/Pad{i}.lean"} for i in range(100)]
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: _FakeProc(stdout=json.dumps([_pr_payload(files=padded)])),
    )
    (pr,) = list_open_prs("alice/proj")
    assert pr.files_truncated is True


# --- protected paths -------------------------------------------------------
#
# Every PR below carries a fully green gate, so a refusal can only be the
# path: a PR that rewrote a workflow or a gate module is exactly the PR whose
# checks say nothing.


@pytest.mark.parametrize("path", [
    ".github/workflows/x.yml",
    "gate/checks.py",
    "lean-toolchain",
    # No prover is passed, so the other provers' manifests are covered too.
    "Session/ROOT",
    "proj.opam",
    # Another spelling of the same file on a case-insensitive checkout.
    ".GitHub/workflows/x.yml",
    "Lakefile.toml",
])
def test_merge_pr_refuses_a_pr_that_changes_a_protected_path(monkeypatch, path) -> None:  # type: ignore[no-untyped-def]
    payload = _pr_payload(
        files=[{"path": "Sample/Foo.lean"}, {"path": path}],
        statusCheckRollup=_green_rollup(),
    )
    calls = _run_gh(monkeypatch, payload)
    with pytest.raises(PRError, match="protected paths") as exc_info:
        merge_pr("o/r", 7)
    assert path in str(exc_info.value)
    assert "Sample/Foo.lean" not in str(exc_info.value)
    assert _merges(calls) == [], "must not call gh pr merge"


def test_merge_pr_names_the_protected_path_before_any_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A red check on a PR that edited the gate is the gate's edited copy
    talking. The refusal has to be about the path, or the feedback sends the
    contributor to fix a check on a PR that is refused whatever it says."""
    payload = _pr_payload(
        files=[{"path": "gate/checks.py"}],
        statusCheckRollup=_green_rollup(except_for="rebuild"),
    )
    _run_gh(monkeypatch, payload)
    with pytest.raises(PRError) as exc_info:
        merge_pr("o/r", 7)
    assert "gate/checks.py" in str(exc_info.value)
    assert "rebuild" not in str(exc_info.value)


@pytest.mark.parametrize(("entry", "path", "refused"), [
    ("blueprint/", "blueprint/roadmap/a.md", True),
    ("blueprint/", "blueprints/x", False),
    ("blueprint/", "docs/blueprint/y", False),
    # A directory written without its slash is still the directory.
    ("blueprint", "blueprint/roadmap/a.md", True),
    ("blueprint", "blueprints/x", False),
    ("blueprint", "docs/blueprint/y", False),
    ("docs/plan.md", "docs/plan.md", True),
    ("docs/plan.md", "docs/plan.md.bak", False),
    ("docs/plan.md", "other/docs/plan.md", False),
    ("blueprint/", "Blueprint/roadmap/a.md", True),
])
def test_merge_pr_holds_a_pr_to_the_projects_own_protected_paths(  # type: ignore[no-untyped-def]
    monkeypatch, base_policy, entry, path, refused
) -> None:
    """An entry is the path it names and what is under it, anchored at the
    repo root."""
    base_policy.text = f'[protected]\npaths = ["{entry}"]\n'
    payload = _pr_payload(files=[{"path": path}], statusCheckRollup=_green_rollup())
    calls = _run_gh(monkeypatch, payload)
    if refused:
        with pytest.raises(PRError, match="protected paths") as exc_info:
            merge_pr("o/r", 7)
        assert path in str(exc_info.value)
        assert _merges(calls) == []
    else:
        merge_pr("o/r", 7)
        assert len(_merges(calls)) == 1


def test_merge_pr_reads_the_projects_protected_paths_at_the_base_commit(  # type: ignore[no-untyped-def]
    monkeypatch, base_policy
) -> None:
    """The head is the contributor's: a PR could take its own files off the
    list in the same diff."""
    payload = _pr_payload(statusCheckRollup=_green_rollup())
    _run_gh(monkeypatch, payload)
    merge_pr("o/r", 7)
    (asked,) = base_policy.asked
    assert f"expr={payload['baseRefOid']}:.choir/verify.toml" in asked
    assert not any(payload["headRefOid"] in arg for arg in asked)


def test_merge_pr_refuses_a_pr_that_renames_a_file_out_of_a_protected_path(  # type: ignore[no-untyped-def]
    monkeypatch, base_policy, rest_listing
) -> None:
    """`gh pr view` names a renamed file by where it went. Judged on that, a
    PR could move a plan file (or `.choir/verify.toml` itself) out of its
    protected directory and show nothing under it."""
    base_policy.text = '[protected]\npaths = ["blueprint/"]\n'
    payload = _pr_payload(
        files=[{"path": "attic/a.md"}], statusCheckRollup=_green_rollup()
    )
    rest_listing.extend(["attic/a.md", "blueprint/roadmap/a.md"])
    calls = _run_gh(monkeypatch, payload)
    with pytest.raises(PRError, match="protected paths") as exc_info:
        merge_pr("o/r", 7)
    assert "blueprint/roadmap/a.md" in str(exc_info.value)
    assert _merges(calls) == []


@pytest.mark.parametrize("break_policy", [
    lambda policy, payload: setattr(policy, "gh_fails", True),
    lambda policy, payload: setattr(policy, "text", '[protected]\npaths = "blueprint/"\n'),
    # A null file under a commit GitHub could not find is not an absent file.
    lambda policy, payload: setattr(policy, "commit_resolves", False),
    # Nor is a file with no text (a tree or a binary at that path), or one
    # GitHub cut short before `[protected]`: what arrived parses, and is not
    # the policy.
    lambda policy, payload: setattr(policy, "file", {}),
    lambda policy, payload: setattr(
        policy, "file", {"text": '[audits.sorry_delta]\npolicy = "report"\n', "isTruncated": True}
    ),
    lambda policy, payload: payload.update(baseRefOid=""),
], ids=[
    "gh-fails", "malformed", "base-commit-unresolved", "not-text", "truncated",
    "no-base-commit",
])
def test_merge_pr_refuses_when_the_base_policy_cannot_be_read(  # type: ignore[no-untyped-def]
    monkeypatch, base_policy, break_policy
) -> None:
    """Fail closed: the PR is clean as far as anyone can tell, but nobody
    can tell what the project protects."""
    payload = _pr_payload(statusCheckRollup=_green_rollup())
    break_policy(base_policy, payload)
    calls = _run_gh(monkeypatch, payload)
    with pytest.raises(PRError, match="could not be read"):
        merge_pr("o/r", 7)
    assert _merges(calls) == []


def test_a_failed_policy_read_reports_what_gh_said_not_the_query_it_sent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The reason is quoted into the refusal and, on an override, into a PR
    comment that stays. `_gh` leads its own message with the command line,
    which for this read is the whole query."""
    payload = _pr_payload(statusCheckRollup=_green_rollup())

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if "graphql" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="gh: HTTP 502\n")
        if "--paginate" in cmd:
            return _FakeProc(stdout="Sample/Foo.lean\n")
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(prs_mod, "_gh", _REAL_GH)
    with pytest.raises(PRError, match="could not be read") as exc_info:
        merge_pr("o/r", 7)
    assert "gh: HTTP 502" in str(exc_info.value)
    assert "repository(" not in str(exc_info.value)


def test_merge_pr_force_merges_past_protected_paths_and_names_them(  # type: ignore[no-untyped-def]
    monkeypatch, base_policy
) -> None:
    """A change to the gate's own files is the overseer's to make, and the
    override is how they make it through a PR — on the record."""
    base_policy.text = '[protected]\npaths = ["blueprint/"]\n'
    payload = _pr_payload(
        files=[
            {"path": "blueprint/roadmap/a.md"},
            {"path": "lean-toolchain"},
            {"path": "Sample/Foo.lean"},
        ],
        statusCheckRollup=_green_rollup(),
    )
    calls = _run_gh(monkeypatch, payload)
    merge_pr("o/r", 7, force="toolchain bump; verified locally")

    (commented,) = [c for c in calls if any("/comments" in part for part in c)]
    body = "\n".join(commented)
    assert "blueprint/roadmap/a.md" in body
    assert "lean-toolchain" in body
    assert "Sample/Foo.lean" not in body
    assert "green" in body, "the checks' own state must still be on the trail"
    assert len(_merges(calls)) == 1


def test_merge_pr_force_records_a_policy_it_could_not_read(  # type: ignore[no-untyped-def]
    monkeypatch, base_policy
) -> None:
    """The override must still work here — a malformed `.choir/verify.toml`
    on the base branch is repaired by a PR that only an override can merge —
    but the trail must not read as though the paths were checked."""
    base_policy.text = "[protected]\npaths = [3]\n"
    payload = _pr_payload(
        files=[{"path": ".choir/verify.toml"}], statusCheckRollup=_green_rollup()
    )
    calls = _run_gh(monkeypatch, payload)
    merge_pr("o/r", 7, force="repair the policy file")

    (commented,) = [c for c in calls if any("/comments" in part for part in c)]
    body = "\n".join(commented)
    assert ".choir/verify.toml" in body
    assert "not fully checked" in body
    assert len(_merges(calls)) == 1
