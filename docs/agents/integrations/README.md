# Integrations — running another tool inside Choir's loop

Choir distributes the work; it does not insist on how the work is planned
or proved. The orchestrator playbook leaves the plan to you
(`ORCHESTRATOR.md` § Plan before you publish), and the contributor manual
leaves the proving to the worker (`CONTRIBUTOR.md` § 3. Prove it). A tool
that plans, orchestrates, or proves can fill either slot while Choir keeps
the rest: tasks, leases, workspaces, pull requests, the gate, and the merge.

When the user names such a tool — "use AutoformBot for orchestrating",
"use AutoformBot as a worker" — read its note here and run the loop with the
tool in the slots the note names. Every step the note does not mention runs
exactly as the playbook says.

| Tool | Note | Slots |
|---|---|---|
| AutoformBot | [`autoform-bot.md`](autoform-bot.md) | planning and statements (overseer), proving (contributor) |

A note answers the same questions for every tool:

- **Setup** — what the tool adds to the project repo, and what it must not
  run on pull requests.
- **The plan** — where it lives, how to read which nodes are ready, and how
  a node becomes a task.
- **Statements** — who writes them, and where. A worker never touches one
  (`CONTRIBUTOR.md` § Boundaries), whichever tool it runs.
- **Each pass** — how to render the plan's claims for `choir orch check-plan`,
  the check that replaces `sync-graph` (§ Holding a plan to the build).
- **After a merge** — how the plan records it.
- **The worker** — what runs inside a claimed workspace, and which of the
  tool's own steps it skips there.

A tool with no note can still fill a slot: the playbook's rules bind it the
same way, and the note is where what you learn about it goes.

A note covers only where the tool meets Choir; for everything else, use the
tool's own documentation. Notes track the tool's current release. When a
change on either side breaks a note, whoever made the change updates it here.

## Holding a plan to the build

The default plan cannot drift for longer than a pass, because `sync-graph`
derives it from the build. A plan Choir does not keep is authored, so each
pass you ask the build whether it still bears the plan out:

    <the tool's own export> | choir orch check-plan <checkout> --plan -

`check-plan` reads the plan's claims, probes the built checkout, and reports
every disagreement. It writes nothing: the plan is the tool's to edit, and
which side is wrong is yours to judge. Like `sync-graph` it refuses a checkout
that is not built at its current source.

The claims are JSON in one shape for every tool, so the translation happens
once, on the tool's side:

```json
{"nodes": [
  {"id": "powers/sum-cube",
   "decls": ["Proj.sum_cube"],
   "stated": true,
   "proved": false,
   "uses": ["linear/sum-to"],
   "proof_uses": ["powers/nicomachus"]}
]}
```

- `id` — the plan's own name for the node. `uses` and `proof_uses` are lists
  of these.
- `decls` — the declarations the node names in the project's source, fully
  qualified. Leave out nodes for results a library already proves: this
  build does not declare them.
- `stated`, `proved` — what the plan claims about the node. The build is read
  one way for every tool: a node whose declarations are all there with no
  placeholder has to say `proved: true`, a definition as much as a theorem,
  or it comes back `proof-not-recorded`. A tool that keeps no proof status
  for a definition says `true` for a stated one.
- `uses` — the nodes the statement needs. `proof_uses` — the ones only the
  proof needs.

All six keys are required on every node, and any other key is ignored.

| Finding | The plan says | The build shows |
|---|---|---|
| `proof-claimed-over-placeholder` | proved | a declaration the node names still holds a placeholder |
| `proof-not-recorded` | stated, not proved | every declaration the node names is there, none with a placeholder |
| `statement-not-recorded` | not stated | a declaration the node names is there |
| `declaration-missing` | stated | a declaration the node names is not there |
| `unplanned-placeholder` | nothing | a declaration holding a placeholder that no node names |
| `duplicate-target` | two nodes name one declaration | — |
| `edge-missing` | no path from one node to another | a declaration of the first reaches a declaration of the second |

The plan has to reach a dependency, not list it: a path of recorded edges is
enough, because what a plan computes from its edges is the same along a path
as along a direct edge. So a finding always means the plan can compute
something false. The build's reach is direct or runs through declarations no
node names, so a helper lemma cannot hide a dependency. A statement's
dependency has to be reached through `uses` alone; a proof's along either
kind of edge. A declaration still holding a placeholder has no proof yet, so
only its statement is read. An edge the plan records and the build does not
show is not a finding.

Declarations no node names and that hold no placeholder are helpers, which
Choir permits; they come back under `helpers`, not `findings`. `agrees` is
true exactly when `findings` is empty, and the exit code is 0 either way.

A structure or an inductive type brings declarations nobody wrote: its
constructor, its projections, its recursors. Each is a declaration of the
build (`choir orch deps` shows them) that no node names, so they come back
under `helpers`, and a term that uses one reaches whatever the type's fields
mention. The type's own node recording those dependencies under `uses` is
what satisfies it.

`choir orch deps <checkout>` prints the build's half on its own, one record
per declaration, for a tool that would rather do the comparing itself.
