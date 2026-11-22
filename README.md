# realm

A shared space for agents to work together.

One SQLite file. Typed nodes. **Two enforcement gates.** Not a chat log, not a task board.

The problem this solves: several CLI coding agents (codex, claude, opencode) work the same
project. Without a substrate they duplicate work, claim the same task twice, and — the expensive
failure — **cite each other's unverified conclusions as if they were established**. `realm` makes
three things structural rather than advisory:

1. A task cannot be completed while a fact it depends on is unverified or tainted.
2. A fact cannot be cited unless it actually passed a check — with a defence against an agent
   registering a self-serving check.
3. Every mutation is an event with a monotonic cursor, so an agent re-learns only what changed.

## Install

Realm uses Python 3.9+ and the standard library. To run agent tasks, install and
sign in to the CLI harnesses you want to use on your own machine. Realm starts those
installed CLIs with the current user’s environment.

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/realm" "$HOME/.local/bin/realm"
```

## Quickstart

```sh
cd my-project
realm init

# work is a node, not a sentence
realm add task "Publish the field guide"
realm add fact "The guide has a title" --check "grep -q '<title>' index.html"

realm link T001 requires F001     # T001 depends on F001
realm take T001                   # lease it (15 min, CAS, fencing token)
realm verify F001                 # runs the check; VERIFIED / REFUTED
realm done T001                   # GATE 1: refuses unless F001 is VERIFIED & untainted
```

`realm state` answers "what should I do next"; `realm since <cursor>` answers "what changed".

## Run a goal across agents

```sh
realm orchestrate --planner codex --workers codex,opencode -- \
  "Summarize the sample order data and have a second agent check the totals"
```

The planner creates one ordered task per selected CLI. Realm leases each task
to its worker and passes earlier findings to the next. Progress appears in the
terminal, and `realm up` shows the shared leases and open tasks. Realm stores
worker findings and the final report under `.realm/runs/` and links them to their
tasks as artifacts. A blocked step leaves its dependent work open. Add `--yolo`
to `realm orchestrate` or `realm dispatch` to use the vendors’ full-access modes.

Use `realm dispatch` to hand an existing task to a worker or retry it.

```sh
task=$(realm add task "Check the sample order totals" --body "Recount the units from orders.csv")
realm dispatch "$task" --agent opencode
```

Supported planner and worker names are `codex`, `opencode`, `claude`, `hermes`,
and `agy`. Use `--timeout SECONDS` to change the five-minute limit for each CLI
invocation. Set `REALM_HERMES_TOOLSETS=file` for a Hermes run that needs the native
file toolset selected explicitly. A Hermes gateway can keep an accepted turn
active after the local CLI stops.

## Commands

| command | purpose |
|---|---|
| `init [--path]` | create `.realm/space.db` |
| `add <kind> <title> [--check CMD] [--path P] [--priority N] [--depends-on ID]` | create a node |
| `take <task> [--ttl SEC]` | lease a task (CAS + fencing token) |
| `verify <fact> [--force]` | run its check; cache by (fact, check, input) |
| `done <task>` | **GATE 1** |
| `cite <fact> [--assume] [--for NODE]` | **GATE 2** |
| `link <src> <kind> <dst>` | typed edge |
| `state [--budget N]` | frontier + leases + taint |
| `since <cursor>` | events after a watermark |
| `show <id>` | full node + edges |
| `run -- <cmd…>` | execute and record an attempt (argv, exit, git rev) |
| `orchestrate <goal> [--planner NAME] [--workers NAME,NAME]` | plan, hand off, and report a goal |
| `dispatch <task> --agent NAME` | run an existing task through one worker CLI |

Kinds: `task` · `fact` · `decision` · `artifact` · `attempt`

## The two gates

**Gate 1 — `done`** refuses (exit 2) unless every fact reached by `requires`/`depends_on` is
`VERIFIED` **and** untainted. Also refuses on an expired lease, even if nobody stole it.

**Gate 2 — `cite`** refuses (exit 2) unverified, refuted, contested, and uncheckable facts.
The interesting cell:

> A fact that passed only a **TRIVIAL** check (e.g. `true`, or exit 0 in under 50 ms with no
> output) is citable **by its author** but **refused for everyone else**.

An agent can fool itself. It cannot fool the others. That is the whole defence against an agent
marking its own work verified.

### Taint

```sh
realm cite F001 --assume        # a hypothesis, explicitly
```

Everything downstream of `F001` becomes **tainted**, transitively, and `done` refuses on any task
depending on a tainted fact. Taint **clears automatically** when `F001` is verified — no manual
step, and no need to re-run the dependent's own check.

A tainted fact with its own passing check is still tainted. Only the root verifying clears it.

## Exit codes

```
0 OK · 1 USAGE · 2 REFUSED(gate) · 3 MISSING · 4 LEASE · 5 VERIFY_FAIL · 7 INTERNAL · 8 UNCHECKABLE
```

`verify` on a failing check returns **5**. Gate refusals return **2**. Do not conflate them.

## Two implementations

| | `realm` (Python) | `realm-rs` (Rust) |
|---|---|---|
| size | 28 KB, stdlib only | 2.5 MB static binary |
| run as | `python3 realm …` or `./realm` | `./realm-rs …` |
| schema | identical (`schema.sql`) | identical, byte-for-byte verified |

Both implement the core commands, gates and exit codes. `REALM_IMPL=rust` delegates core
commands to the binary and `REALM_IMPL=py` selects Python. The `realm` launcher runs
`dispatch` and `orchestrate` in Python against the same space with either setting.

The port exists because the two defects that cost this project the most were **type errors Python
could not see**: a column referenced in code but absent from the schema, and a swapped INSERT
column order that a foreign-key constraint swallowed. In Rust those are compile errors.

## Tests

```sh
# build the optional native implementation before testing both
cargo build --release --locked --manifest-path rust/Cargo.toml

# canonical suite against both implementations
REALM_IMPL=py   python3 -I -B -S -m unittest discover -s tests -p 'test_*.py' -v
REALM_IMPL=rust python3 -I -B -S -m unittest discover -s tests -p 'test_*.py' -v

# native parity — drives the Rust binary directly
python3 -I -B -S -m unittest discover -s tests/rust -p 'test_*.py' -v
```

The tests create disposable projects and SQLite databases. Run the suite from the repository root.

## The UI

```sh
realm ui              # live view; q to quit
realm ui --once       # one frame, useful in scripts
realm ui --once --ascii --cols 100 --rows 30
```

Read-only: it never writes to the space. Loads a single consistent snapshot per frame, so a
concurrent writer cannot produce a torn view.

Layout: a header that answers *is it working / is it stuck / what changed*, an adjacency tree of
what the space believes (dependents nested under prerequisites, severity-sorted), a roster of who
holds what lease, and an evidence tail.

Encoding is **glyph plus spelled reason** — `✗ REFUTED`, `! TAINTED`, `B BLOCKED<-F001` — so
nothing depends on colour and output survives being piped to a file. `--ascii` renders pure ASCII
at every width.

Display limit: the evidence tail loads the most recent 40 events. When a `--since` watermark
predates the oldest loaded event the header says so explicitly —
`12+ changed (partial: tail 40 of a longer history)` — rather than presenting an undercount as a
total.

## Design notes

- **One SQLite file, WAL mode.** No sidecar log, no hash chain — two sources of truth disagree.
- **Typed nodes, not one uniform claim.** Tasks are leased work; facts are checkable assertions;
  decisions have no check, only supersession.
- **Polling (`since`), not push.** No daemon, no orphaned watchers.
- **Edges point dependent → prerequisite.** Taint propagates backwards along that arrow.
- **Enforcement at exactly two boundaries.** Everything else warns. A substrate that refuses too
  much gets routed around; one that refuses where a real conflict exists gets used.

## Known limits

These are **declared gaps**, independently reviewed and not claimed as solved:

- **Scoped-input freshness** — `input_hash` covers `{fact_id, claim, check_hash}` only, **not**
  git rev or scoped file bytes. Changing a source file will not invalidate a cached verify.
- **Explicit done-token fencing** — `done` verifies the lease internally (owner + expiry) but takes
  no `--token` argument, so a zombie writer is stopped by expiry, not by an explicit fencing check.
- **No renewal** — there is no heartbeat; a check longer than its TTL outlives the lease.
- **Execution-output artifacts** — `run` records the attempt (argv, exit, git rev) but attaches no
  output files; `--output` does not exist.
- **No staleness propagation** — `supports` / `contradicts` / `invalidates` are stored and ignored.
- `refresh_taint` writes on every gate read — a contention surface under heavy parallelism.
