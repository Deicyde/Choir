# AutoformBot on Choir

[AutoformBot](https://github.com/facebookresearch/autoform-bot) plans a
formalization as a Markdown blueprint and proves it with local agents. On
Choir it plans and writes statements on the overseer's machine and proves on
contributors' machines, and Choir carries every task between them. Use
AutoformBot's skills as they are; this note covers only where the two tools
meet.

The blueprint is more than a plan to read from. AutoformBot derives what is
ready and what is finished from the articles and nothing else, so an article
has to say what is true of the code after every merge. On Choir you write the
articles and contributors write the proofs, which is why this note has you
check the one against the other each pass.

`$AUTOFORM` is a clone of AutoformBot's `execution` branch, which is where its
worker and specialist agents are. An installed plugin's directory is not a
checkout and will not do. Its CLI runs as
`uv run --project "$AUTOFORM" autoform …` from the project checkout.

## Overseer

### Setup

After Choir's bootstrap, add AutoformBot's layer with `autoform init`, as its
`setup` skill says. Then, before you push:

- Add `site/`, `site-src/` and `*.log` to `.gitignore`; `init` leaves Choir's
  alone.
- **Delete what `init` wrote under `.github/`:** `autoform-verify.yml`,
  `blueprint-pages.yml` and `autoform_audit.py`. The audit rejects `sorryAx`,
  which a Choir project holds for every open task, so it is red on pull
  requests and on every push to the default branch alike. Publishing the
  blueprint to Pages is the overseer's decision, and not one to make by
  pushing a workflow.
- **Protect the blueprint.** Add to `.choir/verify.toml`:

      [protected]
      paths = ["blueprint/"]

  The merge preflight then refuses a pull request that edits an article.
- **Keep one plan.** Do not keep a root `roadmap/` as well: AutoformBot's
  commands that take the project root refuse a tree holding both. Keep your
  reasoning log in `reasoning-log.md`.

### The plan

The blueprint, built with AutoformBot's `roadmap` skill, is the plan.
`orchestrator-planning.md` from § Decomposition guidance on still applies:
read "node" as an article, and `uses` and `proof_uses` as its
`## Depends on` and `## Proof depends on`.

### Statements

You write every statement, using AutoformBot's statement work on this machine;
its claims work here because you have write access. Run its `content-reviewer`
and `counterexample-hunter` on each statement before you commit it: a published
statement is frozen, so a wrong one costs a republish. Commit each declaration
with a `sorry` body, set `statement: formalized` and `lean:` on its article,
and push. Don't run AutoformBot's proof phase on work you publish.

### Publishing

A node is ready when AutoformBot reports it `can_prove` and not yet proved.
That is stricter than Choir's default, which publishes a node once what it
depends on is stated: here a node waits until its proof dependencies are
proved.

```bash
uv run --project "$AUTOFORM" python - <<'EOF'
import json
from autoform_cli.runtime import load_runtime_graph
graph = load_runtime_graph("blueprint", lean_root=".")
for node in graph.nodes:
    if (node.dispatchable and node.status.can_prove and not node.status.proved
            and not node.assertions.not_ready):
        print(json.dumps({"id": node.id, "article": node.article_path,
                          "targets": [t.as_dict() for t in node.lean_targets]}))
EOF
```

Publish one task per declaration still holding a placeholder:
`--target-file` and `--target-decl` from `targets`, `--blueprint-ref` set to
the node id, and the article's path in `--prose`. `create-task` refuses a
declaration that already has an open task, so running this again is safe.
Then set `discussion: <issue number>` on the article; it follows the article
if the article moves, which the path-derived node id doesn't.

### The loop

- **Each pass:** `autoform check blueprint --lean-root .`, then hold the
  blueprint to the build. This replaces `sync-graph`:

  ```bash
  uv run --project "$AUTOFORM" python - <<'EOF' | choir orch check-plan . --plan -
  import json
  from autoform_cli.runtime import load_runtime_graph
  graph = load_runtime_graph("blueprint", lean_root=".")
  print(json.dumps({"nodes": [
      {"id": n.id, "decls": [t.declaration for t in n.lean_targets],
       "stated": n.status.stated, "proved": n.status.proved,
       "uses": list(n.statement_dependencies),
       "proof_uses": list(n.proof_dependencies)}
      for n in graph.nodes if n.formalizable and not n.mathlib]}))
  EOF
  ```

  `autoform check` and `autoform audit` read the articles and trust them;
  this reads the build. Every finding is an article to correct or a merge to
  look at again (`integrations/README.md` § Holding a plan to the build).
- **Review:** use AutoformBot's `agent-review` rubrics in § Stage two, on the
  diff. For what the rubrics ask you to compile or probe, cite the gate's
  `rebuild`, `comparator` and `trust-report` on that head commit; don't build
  a contributor's tree on this machine. Run `autoform skeleton` on the
  default branch after the merge.
- **After a merge:** set `proof: formalized` once every declaration the article
  names is proved, then publish what became ready. Until you do, `check-plan`
  reports the node as `proof-not-recorded`.
- **After you change an article that has an open task:** a workspace holds the
  blueprint at the task's pin, so move the pin with
  `choir orch edit-task <n> --commit <sha>`. It refuses while the task is
  claimed.
- **A reduction:** each child becomes an article beside its parent
  (`declaration:`, `origin: bridged`, `statement: formalized`, `lean: <child>`,
  its statement, and a `## Depends on` section), linked from the parent's
  `## Proof depends on`. Until then `check-plan` reports the child as an
  `unplanned-placeholder`. A worker wrote these statements, so run
  `content-reviewer` on each before you accept.

## Contributor

In § 3. Prove it, hand the task to AutoformBot's `autoform-worker` agent and
act as its parent. Don't run its `orchestrate` skill here: that picks and
claims nodes itself.

- **Project:** `prepared.path`. **Article:** the path `TASK.md` names.
  **Target:** `target_file` and `target_decl` from `prepared.record`. Have it
  read `.choir-context.md`, `CHOIR.md` and `TASK.md` first.
- **Claim:** your Choir lease. Don't run `autoform claim` or take its
  `lake-build` claim; `choir worker heartbeat` is the renewal it expects. It
  says the parent "releases the node claim on every outcome"; that is not
  `choir worker release`, which gives the task up.
- **Tell it:** edit only the target file, not the article, and never touch a
  statement. You commit and run `choir worker submit`; it does neither.

Its `PROVED` or `FAILED` report is the report § 3 asks for. A refutation from
`counterexample-hunter` is a "this statement is wrong" `choir-defect`.
