# realm — agent protocol

realm is a shared space for agents working on the same problem. One SQLite file, typed
nodes, two enforcement gates.

**If it is not a node, it did not happen.** Chat is for the human reading now; nodes are
for every agent that comes after.

## 0. First action

Before anything else, in every session:

```
realm state --budget 300
```

Work from what it returns. If your work already exists as a task, `take` it. Never `add`
a duplicate — search first.

## 1. Your identity

`REALM_AGENT` is your identity. It is used for lease ownership, `created_by`, and the
trivial-check rule. Set it per terminal:

```sh
export REALM_AGENT=opencode     # or codex, claude
```

If it is unset, everything you write is attributed to `unknown` and leases cannot be
fenced. There is no way to undo that attribution after the fact.

## 2. Node types

| Type | Use for |
|---|---|
| `task` | work to do |
| `fact` | a claim about the world |
| `decision` | a choice, with the reason |
| `artifact` | an output that exists on disk |
| `attempt` | a try that failed, so nobody repeats it blind |

## 3. Taking work

```
realm take T004
```

A lease has a TTL and expires **silently**. Finish or renew inside it. No lease, no
changes — the gates check it.

## 4. Recording as you go

Record conclusions when you reach them, not at the end of the session.

```sh
# a conclusion, with the command that proves it
realm add fact "kernel supports init_boot" --check "grep -q init_boot notes.md"
realm verify F001

# a choice, with why
realm add decision "port the reader to Rust" --body "both Python defects were type errors"

# something that exists
realm add artifact "release build" --path dist/RealmLens.app

# a dead end
realm add attempt "tried 192 steps, overfit"
```

## 5. The two gates

**Gate 1 — you cannot finish work that rests on an unproven claim.**

```
realm link T001 requires F001
realm take T001
realm done T001          # REFUSED while F001 is UNVERIFIED
```

`done` also refuses while a dependency is tainted (see §6). Fix the cause, then `done`.

**Gate 2 — you cannot cite a claim nobody checked.**

```
realm cite F001          # REFUSED: UNVERIFIED
realm verify F001        # runs the check
realm cite F001          # ok
```

`cite` refusing is the system working, not a bug. Run the check, verify, cite again.
Never paraphrase an uncited fact into prose as a workaround.

### Trivial checks

A check like `true`, `:`, or anything that exits 0 in under 50 ms with no output is
recorded as `TRIVIAL`. A TRIVIAL fact can be cited **by its author only**. You can
convince yourself; you cannot convince another agent. That asymmetry is intentional.

## 6. Hypotheses

`realm cite F001 --assume` is legal. It marks everything downstream as **tainted**, and
`done` refuses on tainted dependencies. Taint clears automatically once the fact verifies.

Use it to keep working on an open question. Do not use it to make a gate pass.

## 7. Staying current

`realm state` prints a `seq`. Next turn, ask what changed instead of re-reading
everything:

```
realm since 47
```

## 8. Looking at the space

```
realm state --budget 300     # what should I do
realm show F001              # one node in full
realm ui                     # live tree, any terminal
realm up                     # who is working, roped to an orchestrator view
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | usage error |
| 2 | refused by a gate |
| 3 | node not found |
| 4 | lease held by another agent |
| 5 | verification FAILED |
| 7 | internal error |
| 8 | uncheckable |
