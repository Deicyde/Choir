"""Public API for maintainer-side operations on a Choir project repo.

The primitives:

- :func:`create_task_issue`  — create one issue from a TaskRecord
- :func:`list_choir_tasks`   — enumerate existing Choir issues
- :func:`open_task_for_decl` — the open task a declaration already has
- :func:`get_task`           — read one issue back as a TaskRecord
- :func:`set_task_pin`       — move one issue's pinned commit

These are intentionally low-level. Composing them (batch-creating
issues for a benchmark, building a leaderboard, polling status) is
the overseer's job — Choir provides the primitives, not the policies.

All operations go through `gh`; the maintainer is expected to be
authenticated locally (`gh auth login`). Choir does not see or
forward credentials.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from gate.state.task_record import TaskRecord, TaskType
from orchestrator.tasks.serialize import body_to_task, task_to_body

# Stable label conventions used by the intake workflow + lease state machine.
LABEL_TASK = "choir/task"
LABEL_AVAILABLE = "choir/available"


class MaintainerError(RuntimeError):
    """A `gh` / git operation failed in a maintainer-side call."""


@dataclass(frozen=True)
class TaskHandle:
    """Lightweight reference returned by listing operations.

    `record` is None when the issue's body failed intake parse — the
    handle is still returned (number + title + url) so the maintainer
    can surface or fix broken issues from their tooling.
    """

    number: int
    title: str
    url: str
    state: str  # "open" / "closed"
    labels: tuple[str, ...]
    record: TaskRecord | None
    updated_at: str = ""
    """When GitHub last saw activity on the issue, comments included.

    Under spec D4 a claim is a comment and a contributor cannot write
    labels, so nothing about a claim changes anything else this handle
    carries. This is the field that moves."""


@dataclass(frozen=True)
class TaskView:
    """A fully parsed task: handle + record + free-form prose."""

    handle: TaskHandle
    record: TaskRecord
    prose: str


def _gh(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True
        )
        return result.stdout
    except FileNotFoundError as e:
        raise MaintainerError(
            "`gh` CLI not found in PATH — install it from https://cli.github.com"
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise MaintainerError(
            f"gh {' '.join(args)} failed — {stderr}"
        ) from e


def create_task_issue(
    repo: str,
    *,
    task: TaskRecord,
    title: str,
    prose: str = "",
    extra_labels: tuple[str, ...] = (),
) -> int:
    """Create a Choir issue from a TaskRecord. Returns the issue number.

    The body is serialized via :func:`task_to_body`; the result
    round-trips through the intake parser, so the intake workflow's
    label-normalization step will succeed without manual fixup.

    The issue is created with `choir/task` and `choir/available`
    (the intake workflow's idempotency / normalization step will add
    type-specific labels like `choir/type:prove`). Pass `extra_labels`
    to add overseer-specific labels (e.g., `benchmark/formalqual`).

    Pre-existing labels are not required; `gh issue create` accepts
    unknown labels via `--label` only if they exist in the repo, so
    the caller may need to run `scripts/setup-labels.sh` first. This
    function does not implicitly create labels — that's a repo-setup
    concern, not a per-issue concern.
    """
    body = task_to_body(task, prose)
    label_args: list[str] = []
    for lbl in (LABEL_TASK, LABEL_AVAILABLE, *extra_labels):
        label_args.extend(["--label", lbl])
    out = _gh(
        "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        *label_args,
    )
    # `gh issue create` prints the URL of the new issue. Extract the
    # number from the trailing `/issues/<n>` segment.
    url = out.strip().splitlines()[-1].strip()
    try:
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError as e:
        raise MaintainerError(
            f"couldn't parse issue number from gh output: {url!r}"
        ) from e
    return number


def set_task_pin(repo: str, number: int, commit: str) -> None:
    """Rewrite one issue's `project_ref.commit`, leaving its prose intact.

    The body is re-serialized through :func:`task_to_body`, so the result
    round-trips through the intake parser exactly as the original did.
    Editing the front-matter by hand does not carry that guarantee, which
    is why this is a primitive rather than something a caller assembles.
    """
    view = get_task(repo, number)
    moved = view.record.model_copy(
        update={
            "project_ref": view.record.project_ref.model_copy(
                update={"commit": commit}
            )
        }
    )
    _gh(
        "issue", "edit", str(number),
        "--repo", repo,
        "--body", task_to_body(moved, view.prose),
    )


def list_choir_tasks(
    repo: str,
    *,
    state: str = "all",
    task_type: TaskType | None = None,
    label: str | None = None,
    limit: int = 200,
) -> list[TaskHandle]:
    """List Choir issues in `repo`.

    Filters:

    - ``state``: ``"open"``, ``"closed"``, or ``"all"`` (default).
    - ``task_type``: if set, only issues with the matching
      ``choir/type:<value>`` label are returned.
    - ``label``: an arbitrary additional label to require — useful for
      overseer-side labels like ``benchmark/formalqual``.
    - ``limit``: GitHub's listing limit (default 200, max 1000).

    All issues with the ``choir/task`` label are considered Choir
    tasks. Each returned handle includes the parsed `TaskRecord` if
    the body validates, or `None` if it doesn't (the maintainer
    sees the broken issue and can fix it).
    """
    cmd = [
        "issue", "list",
        "--repo", repo,
        "--label", LABEL_TASK,
        "--state", state,
        "--json", "number,title,url,state,labels,body,updatedAt",
        "--limit", str(limit),
    ]
    if task_type is not None:
        cmd.extend(["--label", f"choir/type:{task_type.value}"])
    if label is not None:
        cmd.extend(["--label", label])

    out = _gh(*cmd)
    items = json.loads(out)
    handles: list[TaskHandle] = []
    for it in items:
        labels = tuple(lbl["name"] for lbl in it.get("labels", []))
        body = it.get("body") or ""
        try:
            record, _ = body_to_task(body)
        except ValueError:
            record = None
        handles.append(
            TaskHandle(
                number=it["number"],
                title=it["title"],
                url=it["url"],
                state=it["state"].lower(),
                labels=labels,
                record=record,
                updated_at=it.get("updatedAt") or "",
            )
        )
    return handles


def open_task_for_decl(
    repo: str, target_file: str, target_decl: str, *, limit: int = 1000
) -> tuple[TaskHandle | None, bool]:
    """The open task already targeting `target_decl` in `target_file`, and
    whether every open task was seen.

    The join is on the declaration, not on `blueprint_ref`. A plan node's
    id changes when the plan is reorganised; the declaration is what the
    task is for, so it is the key that survives the plan being renamed
    around it.

    A declaration is the file and the name together, the pair the gate
    resolves. The name alone identifies it only where names are fully
    qualified. A Rocq or Isabelle task carries the short name its file
    declares, so two files can each hold a `helper`, and those are two
    proofs. `target_file` is compared as a `TaskRecord` holds it, which is
    already normalised.

    Only open tasks count. A closed task is finished or retired, and
    publishing its declaration again — after a revert, or for a golf — is
    ordinary. An issue whose body fails the intake parse targets nothing
    and is skipped: no worker can claim it either.

    The second value is the honest half of a `None`. `gh` returns at most
    `limit` issues, so a listing that comes back full may have a duplicate
    past its end; `False` says "none among those listed", which is not
    "none". It is the caller's to decide what an unfinished search permits.

    With several matches the lowest number is returned: the oldest task is
    the one a worker is likeliest to hold.
    """
    handles = list_choir_tasks(repo, state="open", limit=limit)
    matches = [
        h for h in handles
        if h.record is not None
        and (h.record.target_file, h.record.target_decl) == (target_file, target_decl)
    ]
    if matches:
        return min(matches, key=lambda h: h.number), True
    return None, len(handles) < limit


def get_task(repo: str, number: int) -> TaskView:
    """Read one issue and return its parsed task view.

    Raises `MaintainerError` if the issue's body doesn't validate —
    callers that want to handle malformed issues should use
    :func:`list_choir_tasks` (which returns handles with
    `record=None` on failure) and inspect.
    """
    out = _gh(
        "issue", "view", str(number),
        "--repo", repo,
        "--json", "number,title,url,state,labels,body,updatedAt",
    )
    it = json.loads(out)
    labels = tuple(lbl["name"] for lbl in it.get("labels", []))
    body = it.get("body") or ""
    try:
        record, prose = body_to_task(body)
    except ValueError as e:
        raise MaintainerError(
            f"issue #{number} body failed intake parse — {e}"
        ) from e
    handle = TaskHandle(
        number=it["number"],
        title=it["title"],
        url=it["url"],
        state=it["state"].lower(),
        labels=labels,
        record=record,
        updated_at=it.get("updatedAt") or "",
    )
    return TaskView(handle=handle, record=record, prose=prose)
