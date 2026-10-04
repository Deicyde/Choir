"""Tests for `orchestrator.cli`.

Two properties carry the design and are what these pin down.

**The override is a separate subcommand.** Where a harness allowlists by
command prefix, an override sharing `merge`'s prefix would be granted by
any rule granting merges; where a harness only prompts, the two would look
alike in the prompt. `force` is the
overseer's, not the orchestrator's (`orchestrator/prs.py`, `merge_pr`), so
the two must not share a prefix.

**Outcomes live in the payload, not the exit code.** A gate that refuses is
an answer to "should this merge?", so it exits 0 and says so in JSON;
non-zero is reserved for producing no answer at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from gate.state.intake import ParseSuccess, parse_issue_body
from gate.state.lease_comment import ACTION_CLAIM, LeaseClaim, render_lease_comment
from gate.state.task_record import ProjectRef, TaskRecord, TaskType
from orchestrator import cli as cli_mod
from orchestrator import leases as leases_mod
from orchestrator.prs import PRError
from orchestrator.tasks import api as tasks_api
from orchestrator.tasks.serialize import task_to_body


def _pr(number: int = 7) -> object:
    class _P:
        pass

    p = _P()
    p.number = number  # type: ignore[attr-defined]
    return p


def test_merge_has_no_override_argument() -> None:
    """`merge` must not accept the override under any spelling — that is the
    whole reason the two are separate subcommands."""
    for flag in ("--reason", "--force"):
        with pytest.raises(SystemExit):
            cli_mod.main(["--repo", "o/r", "merge", "7", flag, "because"])


def test_merge_override_requires_a_reason() -> None:
    """An override with no attributable reason is exactly what `merge_pr`
    refuses; the CLI should not be able to express it either."""
    with pytest.raises(SystemExit):
        cli_mod.main(["--repo", "o/r", "merge-override", "7"])


def test_merge_passes_no_force_and_override_passes_the_reason(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[object] = []
    monkeypatch.setattr(cli_mod, "get_pr", lambda repo, number: _pr(number))
    monkeypatch.setattr(
        cli_mod, "merge_pr",
        lambda repo, number, **kw: seen.append(kw.get("force")),
    )

    cli_mod.main(["--repo", "o/r", "merge", "7"])
    cli_mod.main(["--repo", "o/r", "merge-override", "7", "--reason", "runner outage"])

    assert seen == [None, "runner outage"]


def test_refused_merge_exits_zero_and_says_so(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """A red gate is a routine answer, not a crash."""
    monkeypatch.setattr(cli_mod, "get_pr", lambda repo, number: _pr(number))

    def _refuse(repo, number, **kw):  # type: ignore[no-untyped-def]
        raise PRError("PR #7 is not green — refusing to merge")

    monkeypatch.setattr(cli_mod, "merge_pr", _refuse)

    rc = cli_mod.main(["--repo", "o/r", "merge", "7"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["merged"] is False
    assert "refusing to merge" in payload["refused"]


def test_unreadable_pr_is_infrastructure_failure_not_a_refusal(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """A PR that cannot be read produced no answer, so it must not be
    reported as the gate having decided something."""
    def _boom(repo, number):  # type: ignore[no-untyped-def]
        raise PRError("could not resolve to a PullRequest")

    monkeypatch.setattr(cli_mod, "get_pr", _boom)

    rc = cli_mod.main(["--repo", "o/r", "merge", "999"])

    assert rc == 2
    assert capsys.readouterr().out == ""


def test_viz_needs_no_repo_and_no_arguments(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The point of the command is that standing in the folder is enough.

    `--repo` is required for every subcommand that talks to GitHub about a
    named project; this one discovers the project from the folder, so a
    required `--repo` would defeat it.
    """
    seen: dict[str, object] = {}

    def _fake(where, *, repo, out, scope):  # type: ignore[no-untyped-def]
        seen.update(where=where, repo=repo, out=out, scope=scope)
        return {"out": "choir-proof-tree.html", "nodes": 3}

    monkeypatch.setattr(cli_mod, "_viz_report", _fake)
    assert cli_mod.main(["viz"]) == 0
    assert seen == {"where": None, "repo": None, "out": None, "scope": "plan"}
    assert json.loads(capsys.readouterr().out)["nodes"] == 3


def test_viz_reports_an_unreadable_folder_as_bad_arguments(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Standing in the wrong directory is the overseer's mistake to fix.

    It is not a GitHub failure, so it takes exit 1 rather than 2.
    """

    def _fake(*_a, **_k):  # type: ignore[no-untyped-def]
        raise cli_mod.ReportError("not a git checkout")

    monkeypatch.setattr(cli_mod, "_viz_report", _fake)
    assert cli_mod.main(["viz"]) == 1
    assert "not a git checkout" in capsys.readouterr().err


# ------------------------------------------------- where an argument may go
#
# Two spellings of the same invocation have to mean the same thing. Both
# have now cost a session: `--json` after the subcommand on the worker CLI,
# and `--pr 82` where this CLI wanted a bare positional.


@pytest.mark.parametrize("argv", [
    ["--repo", "o/r", "task", "7"],
    ["task", "7", "--repo", "o/r"],
    ["task", "--repo", "o/r", "7"],
])
def test_repo_may_be_written_on_either_side_of_the_subcommand(argv, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Appending the flag is what both people and agents reach for first.

    The middle spelling is the one that fails quietly: with `--repo`
    unknown to the subparser, `o/r` is eaten by the `number` positional and
    the error talks about an invalid int.
    """
    monkeypatch.setattr(cli_mod, "get_task", lambda repo, number: {"repo": repo, "number": number})

    assert cli_mod.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"repo": "o/r", "number": 7}


def test_the_number_may_be_written_as_a_flag(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """`salvage` spells it `--pr`, so reaching for `--pr` here is a fair
    guess; it should not be a usage error."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli_mod, "post_comment",
                        lambda repo, number, body: seen.update(number=number, body=body))

    assert cli_mod.main(["--repo", "o/r", "comment", "--pr", "7", "--body", "hi"]) == 0
    assert seen == {"number": 7, "body": "hi"}
    assert json.loads(capsys.readouterr().out)["number"] == 7


def test_omitting_the_number_is_an_error_against_the_subcommand(capsys) -> None:
    """The usage printed has to be the subcommand's. Reported against the
    top-level parser it lists every subcommand instead, which hides the
    argument that was actually missing."""
    with pytest.raises(SystemExit):
        cli_mod.main(["--repo", "o/r", "comment", "--body", "hi"])

    err = capsys.readouterr().err
    assert "choir orch comment" in err
    assert "--pr" in err


@pytest.mark.parametrize("argv", [
    ["--repo", "o/r", "set-priority", "7", "high"],
    ["--repo", "o/r", "set-priority", "--issue", "7", "high"],
])
def test_a_second_positional_still_binds_when_the_number_is_optional(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`set-priority` and `set-difficulty` take a value after the number, so
    making the number optional puts two positionals in a row."""
    monkeypatch.setattr(cli_mod, "set_priority", lambda repo, number, value: None)

    assert cli_mod.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"number": 7, "priority": "HIGH"}


# ------------------------------------------------- one task per declaration
#
# `create-task` is the only thing standing between a re-run publish step and
# two issues for one proof, so these run the command against a board rather
# than against a stubbed lookup.


def _task_body(decl: str = "Sample.foo", *, file: str = "Sample/Foo.lean",
               blueprint_ref: str | None = None, prose: str = "") -> str:
    return task_to_body(TaskRecord(
        choir_task_version=1, type=TaskType.PROVE,
        target_file=file, target_decl=decl,
        project_ref=ProjectRef(repo="o/r", commit="a" * 40,
                               toolchain="leanprover/lean4:v4.34.0"),
        deps=[], blueprint_ref=blueprint_ref,
    ), prose)


class _Board:
    """A repo's issues, answering the `gh` calls the task commands make.

    `comments` is the thread of every issue, as the lease reader's `--jq`
    projection returns it. An edit lands in `issues`, so a test reads the
    body back the way the next worker would.
    """

    def __init__(self, *issues: dict[str, object],
                 comments: list[dict[str, object]] | None = None) -> None:
        self.issues = [
            {"title": "t", "url": "u", "state": "OPEN",
             "labels": [{"name": "choir/task"}], **issue}
            for issue in issues
        ]
        self.comments = comments or []
        self.created: list[str] = []
        self.edited: list[int] = []

    def issue(self, number: int) -> dict[str, object]:
        return next(i for i in self.issues if i["number"] == number)

    def __call__(self, *args: str) -> str:
        def opt(flag: str) -> str:
            return args[args.index(flag) + 1]

        if args[:2] == ("issue", "list"):
            state = opt("--state")
            shown = [i for i in self.issues
                     if state == "all" or str(i["state"]).lower() == state]
            return json.dumps(shown[: int(opt("--limit"))])
        if args[:2] == ("issue", "create"):
            self.created.append(opt("--body"))
            return "https://github.com/o/r/issues/99\n"
        if args[:2] == ("issue", "view"):
            return json.dumps(self.issue(int(args[2])))
        if args[:2] == ("issue", "edit"):
            self.issue(int(args[2]))["body"] = opt("--body")
            self.edited.append(int(args[2]))
            return ""
        if args[0] == "api" and args[2].endswith("/comments"):
            return json.dumps(self.comments)
        raise AssertionError(f"unexpected gh call: {args}")


_CREATE = ["--repo", "o/r", "create-task", "--title", "prove foo",
           "--target-file", "Sample/Foo.lean", "--target-decl", "Sample.foo",
           "--commit", "b" * 40, "--toolchain", "leanprover/lean4:v4.34.0"]


def test_create_task_refuses_a_declaration_that_has_an_open_task(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The existing task was published under another plan id, which is the
    case a check on `blueprint_ref` would wave through."""
    board = _Board({"number": 12, "body": _task_body(blueprint_ref="old-id")})
    monkeypatch.setattr(tasks_api, "_gh", board)

    rc = cli_mod.main([*_CREATE, "--blueprint-ref", "new-id"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["created"] is False
    assert payload["number"] == 12
    assert payload["refused"]
    assert board.created == []


@pytest.mark.parametrize("issue", [
    {"number": 12, "body": _task_body(), "state": "CLOSED"},
    {"number": 12, "body": _task_body("Sample.bar")},
    {"number": 12, "body": _task_body(file="Sample/Bar.lean")},
])
def test_create_task_publishes_when_no_open_task_has_the_decl(issue, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """A closed task is finished or retired, and another declaration in the
    same file is other work. So is the same name in another file: where a
    prover's names are file-local, that is a different proof. None of them
    is a reason to hold a task back."""
    board = _Board(issue)
    monkeypatch.setattr(tasks_api, "_gh", board)

    assert cli_mod.main(_CREATE) == 0

    assert json.loads(capsys.readouterr().out)["created"] is True
    assert len(board.created) == 1


def test_allow_duplicate_publishes_past_an_open_task(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    board = _Board({"number": 12, "body": _task_body()})
    monkeypatch.setattr(tasks_api, "_gh", board)

    assert cli_mod.main([*_CREATE, "--allow-duplicate"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "created": True, "number": 99, "title": "prove foo"}
    assert len(board.created) == 1


def test_create_task_refuses_when_the_open_tasks_could_not_all_be_read(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Publishing on a search that stopped at the listing limit is how a
    duplicate would get through unannounced."""
    board = _Board()
    monkeypatch.setattr(tasks_api, "_gh", board)
    monkeypatch.setattr(cli_mod, "open_task_for_decl", lambda repo, file, decl: (None, False))

    assert cli_mod.main(_CREATE) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is False
    assert "number" not in payload
    assert board.created == []


# ------------------------------------------------- editing a published task
#
# The body `edit-task` writes is the one the next worker's client parses, so
# each edit is read back through the gate's intake parser, not through the
# orchestrator's own reader.

OLD_PROSE = "Induct on `n`.\n\n---\n\nSee `Sample.bar` for the shape."


def _board_with_task(monkeypatch, *comments: dict[str, object], **issue: object) -> _Board:  # type: ignore[no-untyped-def]
    board = _Board(
        {"number": 7, "body": _task_body(blueprint_ref="old-node", prose=OLD_PROSE), **issue},
        comments=list(comments),
    )
    monkeypatch.setattr(tasks_api, "_gh", board)
    monkeypatch.setattr(leases_mod, "_gh", board)
    return board


def _claim(login: str, *, hours_ago: float) -> dict[str, object]:
    seen = datetime.now(UTC) - timedelta(hours=hours_ago)
    return {
        "id": 1, "login": login, "updated_at": seen.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "body": render_lease_comment(LeaseClaim(login=login, action=ACTION_CLAIM, protocol=1)),
    }


def _parsed(body: object) -> tuple[dict[str, object], str]:
    result = parse_issue_body(str(body), expected_repo="o/r")
    assert isinstance(result, ParseSuccess), result
    return result.record.model_dump(), result.body_prose


@pytest.mark.parametrize(("flag", "value"), [
    ("--commit", "b" * 40),
    ("--blueprint-ref", "new-node"),
    ("--prose", "Induct on `n`; `Sample.bar` was renamed `Sample.baz`."),
])
def test_edit_task_changes_one_part_and_keeps_the_rest(flag, value, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    board = _board_with_task(monkeypatch)
    record, prose = _parsed(board.issue(7)["body"])
    old = {"--commit": "a" * 40, "--blueprint-ref": "old-node", "--prose": OLD_PROSE}[flag]

    assert cli_mod.main(["--repo", "o/r", "edit-task", "7", flag, value]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "edited": True, "number": 7,
        "changed": {flag[2:].replace("-", "_"): {"from": old, "to": value}},
    }
    if flag == "--commit":
        record["project_ref"]["commit"] = value  # type: ignore[index]
    elif flag == "--blueprint-ref":
        record["blueprint_ref"] = value
    else:
        prose = value
    assert _parsed(board.issue(7)["body"]) == (record, prose)


def test_edit_task_will_not_move_the_pin_of_a_held_task(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The holder cloned the pinned commit. The prose asked for in the same
    call is not written either: an edit is whole or not at all."""
    board = _board_with_task(monkeypatch, _claim("alice", hours_ago=1))

    rc = cli_mod.main(["--repo", "o/r", "edit-task", "7",
                       "--commit", "b" * 40, "--prose", "new"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["edited"] is False
    assert "alice" in payload["refused"]
    assert board.edited == []


def test_edit_task_corrects_the_plan_ref_and_prose_of_a_held_task(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Neither is part of what the holder claimed, and the pin they did
    claim is the one thing that must come through untouched."""
    board = _board_with_task(monkeypatch, _claim("alice", hours_ago=1))

    assert cli_mod.main(["--repo", "o/r", "edit-task", "7",
                         "--blueprint-ref", "new-node", "--prose", "new"]) == 0

    assert json.loads(capsys.readouterr().out)["edited"] is True
    record, prose = _parsed(board.issue(7)["body"])
    assert (record["blueprint_ref"], prose) == ("new-node", "new")
    assert record["project_ref"]["commit"] == "a" * 40  # type: ignore[index]


def test_naming_the_pin_a_held_task_already_has_is_not_a_refusal(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """What is refused is moving the pin. A script that sets every open task
    to one commit should not be told no by the tasks already on it."""
    board = _board_with_task(monkeypatch, _claim("alice", hours_ago=1))

    assert cli_mod.main(["--repo", "o/r", "edit-task", "7", "--commit", "a" * 40]) == 0

    assert json.loads(capsys.readouterr().out) == {"edited": False, "number": 7, "changed": {}}
    assert board.edited == []


@pytest.mark.parametrize("thread", [
    [],
    [_claim("alice", hours_ago=72)],
])
def test_edit_task_moves_the_pin_when_nobody_holds_the_task(thread, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """No claim, or a claimant quiet past the staleness window: the arbiter
    a claiming worker runs gives the same answer, so there is no holder
    whose clone the move could contradict."""
    board = _board_with_task(monkeypatch, *thread)

    assert cli_mod.main(["--repo", "o/r", "edit-task", "7", "--commit", "b" * 40]) == 0

    assert json.loads(capsys.readouterr().out)["edited"] is True
    assert _parsed(board.issue(7)["body"])[0]["project_ref"]["commit"] == "b" * 40  # type: ignore[index]


@pytest.mark.parametrize("issue", [
    {"labels": []},
    {"state": "CLOSED"},
    {"body": "Something is broken in the build, not a task."},
])
def test_edit_task_refuses_what_is_not_an_open_task(issue, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """An issue that reads, and is not an open Choir task, is an answer:
    exit 0, said in the payload, and the issue left as it was."""
    board = _board_with_task(monkeypatch, **issue)
    before = board.issue(7)["body"]

    rc = cli_mod.main(["--repo", "o/r", "edit-task", "7", "--prose", "new"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["edited"] is False
    assert payload["refused"]
    assert board.issue(7)["body"] == before


def test_edit_task_reports_the_prose_the_issue_will_hold(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The intake parser drops leading blank lines, so what was typed is not
    what the next reader gets. The answer names the latter, and asking for
    the same thing again finds nothing left to change."""
    board = _board_with_task(monkeypatch)
    argv = ["--repo", "o/r", "edit-task", "7", "--prose", "\n\nSee `Sample.baz`.\r\nInduct."]
    held = "See `Sample.baz`.\nInduct."

    assert cli_mod.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["changed"]["prose"]["to"] == held
    assert _parsed(board.issue(7)["body"])[1] == held

    assert cli_mod.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"edited": False, "number": 7, "changed": {}}
    assert board.edited == [7]


@pytest.mark.parametrize("flags", [
    [], ["--commit", "HEAD"], ["--blueprint-ref", "old\n---\nnode"],
])
def test_edit_task_with_nothing_valid_to_write_is_a_usage_error(flags, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """No part named, a commit the intake parser would reject once it was in
    the body, or a plan reference whose own `---` line would end the
    front-matter early: none produced an answer about the task, and a body
    written from the last two is a task nothing can read or repair."""
    board = _board_with_task(monkeypatch)

    assert cli_mod.main(["--repo", "o/r", "edit-task", "7", *flags]) == 1

    assert capsys.readouterr().out == ""
    assert board.edited == []
