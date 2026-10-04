"""PR primitives for the orchestrator agent.

The review/merge half of the orchestrator loop: list open PRs with
their deterministic-audit status, read the diff, post review feedback,
and merge or close — all through the overseer's local `gh` auth.

The merge decision itself is the agent's job (and is governed by the
project's automation level — see `orchestrator.project_config`). These
functions never decide anything; they read and execute.

Choir's trust floor does not rest on branch protection here. Protection is
unavailable on private repos without a paid plan, and once spec D4's
`enforce_admins: false` bootstrap lands the owner — whose auth the
orchestrator uses — is exempt by design. `merge_pr` therefore enforces the
gate itself: it refuses any PR whose blocking checks are not green, or whose
required checks never ran.

Reading the checks is not enough on its own. They are verified by name and
conclusion, so they cannot show that a PR changed *which* checks run — a
stub job carrying a required name, or an existing workflow whose `run:` step
was gutted, both look green. `merge_pr` therefore refuses by path first:
before it reads a single check it refuses any PR whose changed files include
a protected path (`protected_paths`) — the files that decide which audits
run, in every project, plus whatever the project lists under `[protected]`
in `.choir/verify.toml` as the overseer's own.

What that refusal does *not* cover: it knows paths, not content, and it
lives in this preflight, not in the gate — no workflow runs it, so it binds
a merge made through `merge_pr` and nothing else. Approving a held workflow
run happens before any merge is attempted, and a merge pressed in GitHub's
own interface never reaches this module. The protected-paths gate audit of
the 2026-05-07 decision is still unimplemented; this is the orchestrator's
floor until it exists. The orchestrator therefore still reads the diff — see
`docs/agents/ORCHESTRATOR.md` § Boundaries.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, replace
from fnmatch import fnmatchcase

from gate.checks import is_blocking, missing_required
from gate.provers import PROFILES
from gate.state.close_on_merge import parse_closing_refs
from gate.verify.config import (
    VERIFY_CONFIG_RELATIVE_PATH,
    VerifyConfigError,
    parse_verify_config,
)


class PRError(RuntimeError):
    """A `gh` operation failed in a PR primitive."""


@dataclass(frozen=True)
class CheckResult:
    """One status check on a PR head."""

    name: str
    status: str       # e.g. "COMPLETED", "IN_PROGRESS", "QUEUED"
    conclusion: str   # e.g. "SUCCESS", "FAILURE", "" while running


@dataclass(frozen=True)
class PRView:
    """One pull request, with everything the orchestrator needs to act."""

    number: int
    title: str
    url: str
    author: str
    state: str               # "open" / "closed" / "merged"
    body: str
    base_sha: str
    head_sha: str
    labels: tuple[str, ...]
    files: tuple[str, ...]   # changed file paths
    checks: tuple[CheckResult, ...]
    linked_issues: tuple[int, ...]  # from closes/fixes/resolves keywords
    mergeable: str           # "MERGEABLE" / "CONFLICTING" / "UNKNOWN"
    # "owner/name" of the head — a contributor's fork for every ordinary
    # contribution since spec D4. Defaulted so existing construction sites
    # keep working; empty means "gh didn't report it", which callers must
    # treat as "may be a fork" rather than as same-repo.
    head_repo: str = ""
    files_truncated: bool = False

    @property
    def checks_pending(self) -> bool:
        return any(c.status != "COMPLETED" for c in self.checks)

    @property
    def checks_all_green(self) -> bool:
        """True iff every check completed successfully.

        False while anything is still running — callers that want to
        distinguish "failed" from "not finished" check `checks_pending`.
        Vacuously False when there are no checks at all: a repo without
        the gate workflows shouldn't look green.
        """
        if not self.checks:
            return False
        return all(
            c.status == "COMPLETED" and c.conclusion == "SUCCESS"
            for c in self.checks
        )

    def as_dict(self) -> dict[str, object]:
        d = asdict(self)
        for key in ("labels", "files", "linked_issues"):
            d[key] = list(d[key])
        d["checks"] = [asdict(c) for c in self.checks]
        d["checks_all_green"] = self.checks_all_green
        d["checks_pending"] = self.checks_pending
        return d


def _gh(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True
        )
        return result.stdout
    except FileNotFoundError as e:
        raise PRError(
            "`gh` CLI not found in PATH — install it from https://cli.github.com"
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise PRError(f"gh {' '.join(args)} failed — {stderr}") from e


_PR_JSON_FIELDS = (
    "number,title,url,author,state,body,baseRefOid,headRefOid,"
    "labels,files,statusCheckRollup,mergeable,headRepository,headRepositoryOwner"
)

# `gh pr view --json files` truncates at 100 changed files with no error and
# no marker — the same cap five gate CLIs were fixed to paginate past. It
# still matters here: the protected-path refusal (`protected_paths`) reads
# `pr.files`. A PR padded past the cap could push its `.github/` edit out of
# the list the refusal ever sees. `get_pr` therefore reads the paginated REST
# listing instead; the survey-shaped `list_open_prs` keeps the cheap
# one-request view and flags `files_truncated` so a caller cannot mistake a
# truncated list for a complete one.
_FILES_PAGE_CAP = 100


# Terminal commit-status states of a legacy `StatusContext`. GitHub's
# commit-status API has no `status`/`conclusion` split — one `state` carries
# both — so these map onto COMPLETED + the matching conclusion. Anything
# else (notably `pending`) is deliberately absent: a non-terminal state must
# keep reading as not-COMPLETED so it still blocks a merge.
_TERMINAL_STATUS_STATES = frozenset({"SUCCESS", "FAILURE", "ERROR"})


def _parse_pr(it: dict[str, object]) -> PRView:
    checks: list[CheckResult] = []
    for c in it.get("statusCheckRollup") or []:  # type: ignore[union-attr]
        # gh mixes CheckRun and StatusContext shapes in the rollup;
        # normalize to name/status/conclusion with safe fallbacks.
        status = str(c.get("status") or "")
        conclusion = str(c.get("conclusion") or "")
        if not status:
            # StatusContext shape: no `status` field at all, one `state`
            # holding a terminal commit-status value. Left un-normalized, a
            # green third-party status (Codecov, DCO) read as
            # blocking-and-never-COMPLETED and wedged every merge
            # permanently, with `force` as the only escape.
            state = str(c.get("state") or "").upper()
            if state in _TERMINAL_STATUS_STATES:
                status, conclusion = "COMPLETED", state
            else:
                status = state
        checks.append(
            CheckResult(
                name=str(c.get("name") or c.get("context") or "unknown"),
                status=status,
                conclusion=conclusion,
            )
        )
    body = str(it.get("body") or "")
    author = it.get("author") or {}
    return PRView(
        number=int(it["number"]),  # type: ignore[arg-type]
        title=str(it["title"]),
        url=str(it["url"]),
        author=str(author.get("login", "")),  # type: ignore[union-attr]
        state=str(it["state"]).lower(),
        body=body,
        base_sha=str(it.get("baseRefOid") or ""),
        head_sha=str(it.get("headRefOid") or ""),
        labels=tuple(
            lbl["name"] for lbl in it.get("labels") or []  # type: ignore[union-attr,index]
        ),
        files=tuple(
            f["path"] for f in it.get("files") or []  # type: ignore[union-attr,index]
        ),
        checks=tuple(checks),
        linked_issues=tuple(parse_closing_refs(body)),
        mergeable=str(it.get("mergeable") or "UNKNOWN"),
        head_repo=_head_repo(it),
        files_truncated=len(it.get("files") or []) >= _FILES_PAGE_CAP,  # type: ignore[arg-type]
    )


def _head_repo(it: dict[str, object]) -> str:
    """`"owner/name"` of the PR's head repository, or `""` if gh didn't say.

    gh reports the two halves separately (`headRepository` is the bare name,
    `headRepositoryOwner` the login), and either can be null on a PR whose
    fork has since been deleted.
    """
    name = (it.get("headRepository") or {}).get("name") or ""  # type: ignore[union-attr]
    owner = (it.get("headRepositoryOwner") or {}).get("login") or ""  # type: ignore[union-attr]
    return f"{owner}/{name}" if owner and name else ""


def list_open_prs(repo: str, *, limit: int = 100) -> list[PRView]:
    """All open PRs in `repo`, newest first (gh default ordering)."""
    out = _gh(
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", _PR_JSON_FIELDS,
        "--limit", str(limit),
    )
    return [_parse_pr(it) for it in json.loads(out)]


def get_pr(repo: str, number: int) -> PRView:
    """One PR by number (any state), with every changed file GitHub lists.

    The single-PR read is the one the merge preflight and the stage-two
    review use, so its `files` must be complete: the protected-path refusal
    (`protected_paths`) reads it. `gh pr view --json files` is incomplete in
    two ways, both of them a way past that refusal. It stops at 100 files
    with no marker, so a padded PR could push its `.github/` edit out of the
    list; and it names a renamed file by its new path only, so a PR could
    move a file out of a protected directory and show nothing under it. The
    file list therefore always comes from the REST listing
    (`_all_changed_files`), on a PR of any size. That is one extra request,
    which is the right trade here and the wrong one in `list_open_prs`.
    """
    out = _gh(
        "pr", "view", str(number),
        "--repo", repo,
        "--json", _PR_JSON_FIELDS,
    )
    it = json.loads(out)
    return replace(_parse_pr(it), files=tuple(_all_changed_files(repo, number)),
                   files_truncated=False)


def _all_changed_files(repo: str, number: int) -> list[str]:
    """Every path the PR changes, paginated past the 100-file cap.

    A renamed file contributes both its paths. The old one is where the
    file stops existing, which is as much a change to that path as an edit:
    moving `.choir/verify.toml` aside removes the policy, and moving a plan
    file out of a protected directory deletes it from the plan.
    """
    out = _gh(
        "api", "--paginate",
        f"repos/{repo}/pulls/{number}/files",
        "-X", "GET", "-f", "per_page=100",
        "--jq", ".[] | .filename, (.previous_filename // empty)",
    )
    return list(dict.fromkeys(line for line in out.splitlines() if line.strip()))


def get_pr_diff(repo: str, number: int) -> str:
    """The PR's unified diff, as text."""
    return _gh("pr", "diff", str(number), "--repo", repo)


def post_comment(repo: str, number: int, body: str) -> None:
    """Post a conversation comment on an issue or a PR.

    A PR *is* an issue to the API, and this is the endpoint `get_pr_comments`
    already reads, so one number is enough — the orchestrator answers a task
    and a submission the same way.
    """
    _gh(
        "api", f"repos/{repo}/issues/{number}/comments",
        "-f", f"body={body}", "--silent",
    )


def blocking_failures(pr: PRView, *, prover: str | None = None) -> list[str]:
    """Blocking checks on `pr` that ran but have not completed successfully.

    Pending (including `QUEUED`) counts as a failure for merge purposes: a
    check still running or waiting to run has not vouched for anything.
    Advisory checks are ignored entirely — see `gate.checks`. A required
    check that never ran at all is a different situation, not covered here
    — see `missing_required_checks`.

    `prover` is forwarded to `is_blocking` unchanged (never defaulted to a
    specific prover here) — an omitted prover must get the strict answer,
    same as `is_blocking` itself. The caller resolves the project's prover
    once per loop and passes it in; this function does not read
    `.choir/project.toml` itself, keeping this module a thin `gh` wrapper.
    """
    return [
        c.name
        for c in pr.checks
        if is_blocking(c.name, prover=prover)
        and not (c.status == "COMPLETED" and c.conclusion == "SUCCESS")
    ]


def pending_blocking_checks(pr: PRView, *, prover: str | None = None) -> list[str]:
    """Blocking checks on `pr` that have not finished running.

    The half of `blocking_failures` that is "not yet" rather than "no": a
    caller deciding whether to come back later wants this, a caller deciding
    whether to merge wants `blocking_failures` (which counts pending as a
    failure, because a check still running has vouched for nothing).

    Advisory checks are excluded deliberately: a check that never blocks a
    merge should not hold a green PR pending either, or a slow-but-advisory
    workflow could keep review waiting on a verdict nothing acts on. (Note
    14 §8 promoted `comparator` to blocking; its own `timeout-minutes: 60`
    is exactly the cost of the audit now being load-bearing, not something
    this function excuses it from.)

    `prover` is forwarded to `is_blocking` unchanged, same rule as
    `blocking_failures` above: an omitted prover gets the strict answer.
    """
    return [
        c.name
        for c in pr.checks
        if is_blocking(c.name, prover=prover) and c.status != "COMPLETED"
    ]


def missing_required_checks(pr: PRView) -> list[str]:
    """Required checks (`gate.checks.REQUIRED_PRESENT`) absent from `pr` entirely.

    Distinct from `blocking_failures`: a check that ran and failed means the
    code is unsound or evasive; a check that never ran at all — e.g. because
    its workflow file was deleted on the PR's own head branch, so the
    workflow simply never triggers for that PR — means no audit happened at
    all. Both must refuse a merge, but they're different diagnoses, so the
    preflight names them apart rather than folding one into the other.
    """
    return missing_required(c.name for c in pr.checks)


# The paths `docs/agents/ORCHESTRATOR.md` § Boundaries names, spelled the way
# `[protected] paths` entries are and matched the same way (`_is_protected`).
# All are anchored at the repo root, which is where GitHub reads workflows
# from and where the bootstrap puts `.choir/`, the gate's own code and its
# `pyproject.toml`.
_BUILTIN_PROTECTED_PATHS = (".github/", ".choir/", "gate/", "pyproject.toml")

# The toolchain and manifest files, taken from the prover profiles so the
# worker's primer (`client/workspace.py`) and this refusal cannot name two
# different sets. Every prover's, not the project's own: this module is
# called without a prover as readily as with one, and an omitted prover must
# get the strict answer. A lean4 project has no `ROOT` for a PR to touch, so
# the union costs nothing where it does not apply.
_PROVER_PROTECTED_FILES = tuple(
    sorted({name for profile in PROFILES.values() for name in profile.protected_files})
)

_PROTECTED_POLICY_QUERY = """\
query($owner: String!, $name: String!, $oid: GitObjectID!, $expr: String!) {
  repository(owner: $owner, name: $name) {
    commit: object(oid: $oid) { oid }
    file: object(expression: $expr) { ... on Blob { text isTruncated } }
  }
}"""


@dataclass(frozen=True)
class ProtectedPaths:
    """Which protected paths a PR changes, and whether that answer is whole.

    `touched` is every changed path found to be protected. `incomplete` is
    `None` when `touched` is the full answer, and otherwise says why it may
    not be — the file list was truncated, or the project's own list could
    not be read. The two are separate fields because an incomplete answer
    is not a clean one: a caller that read only `touched` would take "none
    found" for "none there", which is the fail-open this exists to prevent.
    `clear` is the one question both the merge and the triage ask.
    """

    touched: tuple[str, ...]
    incomplete: str | None = None

    @property
    def clear(self) -> bool:
        return not self.touched and self.incomplete is None


def _is_protected(path: str, project_paths: tuple[str, ...]) -> bool:
    # An entry covers the path it names and everything under it, with or
    # without the trailing slash. A directory written as `blueprint` is the
    # likeliest slip in a hand-written list, and read as an exact path it
    # would protect nothing while looking like protection. Nothing is lost
    # by reading it as both: a path is a file or a directory, never the two.
    #
    # Case is folded on both sides. Git keeps `Blueprint/a.md` and
    # `blueprint/a.md` apart, but a checkout on a case-insensitive
    # filesystem does not: pulling the first overwrites the second. Refusing
    # a spelling that would have been harmless elsewhere costs nothing.
    folded = path.casefold()
    for entry in (*_BUILTIN_PROTECTED_PATHS, *project_paths):
        place = entry.rstrip("/").casefold()
        if folded == place or folded.startswith(place + "/"):
            return True
    # A profile's entries are file names, one of them a pattern (`*.opam`),
    # and they are matched at any depth: an Isabelle session directory
    # carries its own `ROOT`, and a Lean package nested in the repo its own
    # lakefile, both as much a part of the build as the one at the root.
    name = folded.rsplit("/", 1)[-1]
    return any(
        fnmatchcase(name, pattern.casefold()) for pattern in _PROVER_PROTECTED_FILES
    )


def _project_protected_paths(repo: str, base_sha: str) -> tuple[str, ...]:
    """`[protected] paths` from `.choir/verify.toml` at the PR's base commit.

    Read from the base, never the head, for the reason every reader of that
    file gives: a PR must not be able to loosen the policy it is judged by.

    Read through `gh`, not from a checkout. This module is a thin `gh`
    wrapper and reads no local config, and that rule is load-bearing here
    rather than tidy: the overseer's checkout is at whatever commit it was
    last pulled to, which need not be this PR's base, and `merge_pr` already
    refuses to trust a PR view it did not fetch itself because a stale view
    is a bypass. A list handed in by the caller is the same shape — a caller
    that forgot it would pass nothing and protect nothing, silently. So the
    function that fetched `pr.base_sha` asks GitHub for the file at exactly
    that commit.

    One GraphQL read rather than the contents endpoint, because absence has
    to be told apart from failure. A project with no `.choir/verify.toml`
    has no paths of its own, which is an answer; a file that could not be
    fetched is not. The contents endpoint answers 404 for both a missing
    file and a commit it cannot resolve, and telling them apart there means
    reading an error string. Here the commit and the file are two fields of
    one response: a null file under a commit that resolved is absence, and
    everything else raises.

    Raises `PRError` when the policy cannot be established — `gh` failed,
    the commit did not resolve, the file is not readable text, or it does
    not parse. The caller refuses on that; an unreadable policy is not an
    empty one.
    """
    if not base_sha:
        raise PRError("gh reported no base commit for the PR")
    owner, _, name = repo.partition("/")
    try:
        out = _gh(
            "api", "graphql",
            "-f", f"query={_PROTECTED_POLICY_QUERY}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-f", f"oid={base_sha}",
            "-f", f"expr={base_sha}:{VERIFY_CONFIG_RELATIVE_PATH.as_posix()}",
        )
    except PRError as e:
        # `_gh` leads its message with the command line it ran, which here is
        # the whole query. This reason is quoted into the refusal and, on an
        # override, into a PR comment that stays, so say what `gh` said.
        failed = e.__cause__
        if not isinstance(failed, subprocess.CalledProcessError):
            raise
        raise PRError(f"gh api graphql failed — {(failed.stderr or '').strip()}") from e
    try:
        repository = json.loads(out)["data"]["repository"]
        commit, file = repository["commit"], repository["file"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise PRError(f"unreadable response from GitHub: {out.strip()!r}") from e
    if not commit:
        raise PRError(f"base commit {base_sha} did not resolve in {repo}")
    if file is None:
        return ()
    text = file.get("text")
    if not isinstance(text, str) or file.get("isTruncated"):
        raise PRError(f"{VERIFY_CONFIG_RELATIVE_PATH} at {base_sha} is not readable text")
    try:
        return parse_verify_config(text).protected.paths
    except VerifyConfigError as e:
        raise PRError(f"{VERIFY_CONFIG_RELATIVE_PATH} at {base_sha} is malformed: {e}") from e


def protected_paths(repo: str, pr: PRView) -> ProtectedPaths:
    """The protected paths `pr` changes — the answer `merge_pr` refuses on.

    Protected means: `.github/`, `.choir/`, `gate/`, `pyproject.toml`, every
    prover profile's toolchain and manifest files, and the project's own
    `[protected] paths` at the PR's base commit. The first group is not
    configurable, for the reason the prover profiles are not: those files
    decide which audits run, and a project that could take one off the list
    could be talked into it.

    Never raises for a policy it could not read; it reports that in
    `incomplete` alongside whatever the built-in set already found, because
    the built-in half of the answer does not depend on the project's file
    and a caller naming what a PR touched should not lose it. Costs one `gh`
    request.
    """
    reasons: list[str] = []
    if pr.files_truncated:
        reasons.append(
            f"only the first {_FILES_PAGE_CAP} changed files were listed — "
            "read this PR on its own for the full list"
        )
    try:
        project_paths = _project_protected_paths(repo, pr.base_sha)
    except PRError as e:
        project_paths = ()
        reasons.append(f"the project's `[protected]` paths could not be read — {e}")
    return ProtectedPaths(
        touched=tuple(sorted(f for f in pr.files if _is_protected(f, project_paths))),
        incomplete="; ".join(reasons) or None,
    )


def merge_pr(
    repo: str,
    number: int,
    *,
    method: str = "squash",
    delete_branch: bool = True,
    force: str | None = None,
    prover: str | None = None,
) -> None:
    """Merge the PR, refusing unless it changes no protected path and every
    required check is present and green.

    `prover` is forwarded to `blocking_failures` unchanged (never defaulted
    to a specific prover) — the caller resolves the project's prover once
    per loop and passes it in, so an omitted prover gets the strict
    (base `gate.checks.CHECKS`) answer rather than silently un-gating a
    prover this function has never heard of.

    The preflight is not redundant with branch protection. Protection is
    unavailable on free private repos, and once spec D4's
    `enforce_admins: false` bootstrap lands the orchestrator runs on the
    owner's auth and the owner is exempt, so GitHub does not constrain this
    call at any automation level — this check is the enforcement.

    Protected paths are refused first, before any check is read
    (`protected_paths`). The checks are judged by name and conclusion, so on
    a PR that changed which checks run they are not evidence of anything,
    and reading them first would report "green" about a gate the PR itself
    rewrote. A project policy that cannot be read refuses too. The bound of
    this refusal is in the module docstring: paths, not content.

    The PR view is fetched here rather than accepted from the caller: a stale
    view is a bypass. A required check that never ran at all (its workflow
    was deleted on the PR's own branch) is refused exactly like a failing
    one — see `missing_required_checks`. The merge itself is pinned to the
    exact head commit the checks were just read against
    (`--match-head-commit`), so a push landing between the read and the
    merge cannot ride through underneath a check that was genuinely green a
    moment ago.

    **`force` is overseer-authorized.** It exists for the human who owns the
    project, not for the orchestrator's own judgment: an autonomous agent
    that overrides its only enforcement when a check looks inconvenient has
    no enforcement. A check that stays red for reasons outside the diff is an
    escalation, and the orchestrator keeps working everything else meanwhile
    (`docs/agents/ORCHESTRATOR.md` § Automation levels, § Boundaries).

    `force` takes a non-blank reason string, never a bare truthy value — the
    obvious wrong idiom after seeing this function refuse is `force=True`,
    which would merge silently with no attributable reason, so that and any
    non-string or blank value are rejected outright. The reason, plus
    whatever it overrode, is posted to the PR before merging — protected
    paths the PR changes included, since a change to the gate's own files is
    the overseer's to make and this is how they make it through a PR — so an
    override is never silent and remains auditable after the fact — and the override
    path is pinned to `pr.head_sha` too, so the commit that merges is the one
    the trail names. The posted state names the prover the override was
    resolved against: `blocking_failures`/`missing_required_checks` only
    surface checks that are actually blocking *for that prover*, so a check
    that is visibly red but relaxed to advisory there (`statement-equiv` on
    lean4) would otherwise vanish from the trail entirely — "all required
    checks were green" is true of the resolved answer, not of every check's
    literal conclusion, and a human auditing the override wants to see that
    distinction, not have it silently folded away. `method` is one of
    "squash" / "merge" / "rebase".
    """
    if method not in {"squash", "merge", "rebase"}:
        raise PRError(f"invalid merge method: {method!r}")

    if force is not None and (not isinstance(force, str) or not force.strip()):
        raise PRError(
            f"force requires a non-blank reason string, not {force!r} — "
            "a merge override must be attributable."
        )

    pr = get_pr(repo, number)
    protected = protected_paths(repo, pr)

    if force is None:
        if protected.touched:
            raise PRError(
                f"PR #{number} changes protected paths: "
                f"{', '.join(protected.touched)} — refusing to merge. These "
                "are the files that decide which audits run, or that the "
                "project's `[protected]` policy keeps for the overseer, so "
                "the checks on this PR are not evidence about it. Reject it "
                "and re-publish the clean task; a change that is genuinely "
                "needed there is the overseer's to make."
            )
        if protected.incomplete:
            raise PRError(
                f"PR #{number} could not be checked against the protected "
                f"paths: {protected.incomplete} — refusing to merge. An "
                "unreadable policy is not an empty one. Fix "
                "`.choir/verify.toml` on the protected branch if it is "
                "malformed; otherwise escalate to the overseer."
            )
        if not pr.checks:
            raise PRError(
                f"PR #{number} has no checks — refusing to merge. A repo "
                "without the gate workflows must not look mergeable."
            )
        missing = missing_required_checks(pr)
        if missing:
            raise PRError(
                f"PR #{number} is missing required checks entirely: "
                f"{', '.join(missing)} — refusing to merge. A check that "
                "never ran (e.g. its workflow was removed on this branch) "
                "is not evidence of passing. If the PR touches `.github/`, "
                "reject it; otherwise escalate to the overseer."
            )
        failures = blocking_failures(pr, prover=prover)
        if failures:
            raise PRError(
                f"PR #{number} has blocking checks not green: "
                f"{', '.join(sorted(failures))} — refusing to merge. Read the "
                "failing check and either give the contributor actionable "
                "feedback or reject the PR. A check that stays red for "
                "reasons outside the diff (infrastructure, a broken runner) "
                "is an escalation to the overseer, not something to work "
                "around: only an overseer authorizes an override."
            )
    else:
        missing = missing_required_checks(pr)
        failures = blocking_failures(pr, prover=prover)
        # Checks that are visibly red (or never completed) but did NOT make
        # `failures` — because they're genuinely advisory, or because this
        # prover's `PROVER_OVERRIDES` relaxed them — are invisible to the
        # override trail otherwise. A human auditing the override wants to
        # know a check was red even though it wasn't blocking, so name it.
        non_blocking_red = sorted(
            c.name
            for c in pr.checks
            if not (c.status == "COMPLETED" and c.conclusion == "SUCCESS")
            and c.name not in failures
        )
        overridden: list[str] = []
        if protected.touched:
            overridden.append(f"protected paths changed: {', '.join(protected.touched)}")
        if protected.incomplete:
            overridden.append(f"protected paths not fully checked: {protected.incomplete}")
        checks_overridden = bool(missing or failures)
        if missing:
            overridden.append(f"missing entirely: {', '.join(missing)}")
        if failures:
            overridden.append(f"failing: {', '.join(sorted(failures))}")
        basis = f"prover={prover!r}" if prover is not None else "no prover given (strict)"
        # Said even when something else was overridden: a PR merged past the
        # protected-path refusal alone had a green gate, and the trail should
        # not leave a reader to infer that from the absence of a clause.
        if not checks_overridden:
            overridden.append(f"all required checks were green (resolved for {basis})")
        state = "; ".join(overridden)
        if non_blocking_red:
            state += (
                f"; non-blocking red under that resolution: "
                f"{', '.join(non_blocking_red)}"
            )
        post_comment(
            repo,
            number,
            f"**Merge preflight overridden.** Reason: {force}\n"
            f"Overridden state at {pr.head_sha}: {state}.",
        )

    args = ["pr", "merge", str(number), "--repo", repo, f"--{method}"]
    # Under spec D4 the head branch lives on the contributor's fork, which
    # the orchestrator has no write access to. Asking gh to delete it there
    # can fail *after* the merge has already landed, turning a successful
    # merge into a raised PRError — the worst shape of failure, since the
    # obvious response (re-run the merge) then hits an already-merged PR.
    # An unknown head repo (`""`, e.g. the fork was deleted) is treated as
    # cross-repo for the same reason: skipping a cleanup is free, failing a
    # completed merge is not.
    if delete_branch and pr.head_repo == repo:
        args.append("--delete-branch")
    # Pinned on both paths. On the force path the audit trail *is* the whole
    # justification, and the override comment names a specific head SHA — if
    # a push landed in between, an unpinned merge would land a different
    # commit than the one the trail claims was overridden.
    args.extend(["--match-head-commit", pr.head_sha])
    _gh(*args)


def close_pr(repo: str, number: int, *, comment: str | None = None) -> None:
    """Close the PR without merging, optionally leaving a final comment."""
    args = ["pr", "close", str(number), "--repo", repo]
    if comment:
        args.extend(["--comment", comment])
    _gh(*args)


def get_pr_comments(repo: str, number: int) -> list[tuple[str, str]]:
    """(author_login, body) for each conversation comment on a PR.

    PR conversation comments are issue comments in the GitHub API.
    Review-thread (diff) comments are deliberately not included.
    """
    out = _gh("api", f"repos/{repo}/issues/{number}/comments")
    items = json.loads(out)
    return [(it["user"]["login"], it.get("body") or "") for it in items]
