"""realm up — agent panes roped to an orchestrator view.

Answers a different question from `realm ui`:

    realm ui   what does the space believe?        -> adjacency tree
    realm up   who is working, on what, and is
               anyone stalled?                     -> roster joined to one column

Read-only. Never writes, never refreshes the taint cache, never recovers a journal.

The rope is a live view of LEASES, not of the process table. An agent can be alive and
idle, and a rope that lit up merely because something was running would be noise. That
is the same rule the macOS app follows, so the two views never disagree.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_AGENTS = "opencode,codex,claude"
FRONTIER_CAP = 8
ATTENTION_CAP = 5
MIN_COLS = 80
MIN_ROWS = 24

# Glyphs and their ASCII fallbacks, kept together so --ascii is one switch.
G = {
    "verified": ("✓", "V"),
    "unverified": ("?", "?"),
    "refuted": ("✗", "X"),
    "contested": ("!", "!"),
    "tainted": ("!", "!"),
    "blocked": ("B", "B"),
    "open": ("·", "."),
    "done": ("✓", "V"),
    "active_dot": ("●", "*"),
    "idle_dot": ("·", "."),
    "vbar": ("│", "|"),
    "hbar": ("─", "-"),
    "corner_tl": ("┌", "+"),
    "corner_bl": ("└", "+"),
    "corner_tr": ("┐", "+"),
    "corner_br": ("┘", "+"),
    "tee": ("├", "+"),
    "arrow": ("▶", ">"),
    "arrow_left": ("←", "<"),
    "ellipsis": ("…", "~"),
}

ASCII = False


def g(key: str) -> str:
    a, b = G[key]
    return b if ASCII else a


def clip(s: str, n: int) -> str:
    s = s.replace("\n", " ").replace("\t", " ")
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    if n == 1:
        return s[:1]
    return s[: n - 1] + g("ellipsis")


def age(ts, now: int) -> str:
    if not ts:
        return "-"
    d = max(0, now - int(ts))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def span(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


# ---------------------------------------------------------------- space


class SpaceError(Exception):
    """A read failed in a way the caller must surface, not paper over."""


def find_space() -> Path:
    env = os.environ.get("REALM_SPACE")
    if env:
        return Path(env)
    here = Path.cwd()
    for p in [here, *here.parents]:
        cand = p / ".realm"
        if cand.is_dir():
            return cand
    raise SpaceError("no space found. run `realm init`.")


def load(sp: Path) -> dict:
    """One consistent snapshot. Raises SpaceError rather than rendering a blank frame."""
    db = sp / "space.db"
    if not db.is_file():
        raise SpaceError(f"space not initialised: {db} does not exist")

    # A hot journal means a writer crashed mid-transaction. Opening the database would
    # make SQLite roll it back, which is a WRITE triggered by looking. Refuse instead.
    journal = Path(str(db) + "-journal")
    try:
        if journal.is_file() and journal.stat().st_size > 0:
            raise SpaceError(
                f"database needs recovery ({journal.name} is {journal.stat().st_size} bytes); "
                "refusing to open it read-only. Let a writer finish or run `realm state`.")
    except OSError as e:
        raise SpaceError(f"cannot inspect journal: {e}") from e

    # A mode=ro handle cannot create -shm, which a WAL database needs. After a writer
    # checkpoints and cleans up, every read on that handle fails with "attempt to write a
    # readonly database" — which is a sidecar problem, not a corrupt space.
    #
    # Fallback: READWRITE + query_only, but only after the hot-journal check above, because
    # query_only blocks statement writes and NOT SQLite's recovery pass at open time.
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        con.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error:
        try:
            con.close()
        except Exception:
            pass
        if journal.is_file() and journal.stat().st_size > 0:
            raise SpaceError(f"database needs recovery ({journal.name} appeared while "
                             f"opening); refusing to touch it")
        try:
            con = sqlite3.connect(str(db), timeout=5.0)
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as e:
            if not os.access(db.parent, os.W_OK):
                raise SpaceError(
                    f"cannot read {db.name}: {db.parent} is not writable and SQLite must "
                    f"create the -shm sidecar to read a WAL database") from e
            raise SpaceError(f"cannot open {db.name}: {e}") from e
    con.row_factory = sqlite3.Row
    now = int(time.time())
    try:
        # BEGIN DEFERRED gives one snapshot across every query. Without it a writer can
        # commit a node and its edge between two reads, yielding edges that reference a
        # node this frame never saw.
        con.execute("BEGIN")

        ver = con.execute("PRAGMA user_version").fetchone()[0]
        if ver != 2:
            raise SpaceError(f"unsupported schema version {ver} (this build reads 2)")

        nodes = {}
        for r in con.execute(
                "SELECT n.id, n.kind, n.title, n.created_at, n.updated_at,"
                "       COALESCE(t.state,'') st, COALESCE(t.priority,50) prio,"
                "       COALESCE(f.status,'') fst, COALESCE(f.check_strength,'') fstr,"
                "       COALESCE(f.assumed,0) assumed"
                "  FROM nodes n"
                "  LEFT JOIN tasks t ON t.node_id = n.id"
                "  LEFT JOIN facts f ON f.node_id = n.id"):
            nodes[r["id"]] = dict(r)

        edges = [dict(r) for r in con.execute("SELECT from_id,to_id,kind FROM edges")]
        leases = [dict(r) for r in con.execute(
            "SELECT resource,owner,expires_at FROM leases")]
        activity = [dict(r) for r in con.execute(
            "SELECT actor,detail,node_id,op FROM (SELECT actor,detail,node_id,op,cursor FROM events ORDER BY cursor DESC LIMIT 120) "
            "WHERE op IN ('agent-start','agent-output','agent-end','agent-queued') ORDER BY cursor DESC")]
        # The coordinator owns the goal lease while the selected planner CLI emits
        # its activity. Match the goal and run ID so simultaneous runs stay distinct.
        planners = {r["resource"]: (r["owner"], r["actor"]) for r in con.execute(
            "SELECT l.resource,l.owner,e.actor FROM leases l JOIN events e ON e.node_id=l.resource "
            "WHERE l.expires_at>? AND e.op='agent-start' AND e.actor!=l.owner "
            "AND instr(l.owner,'/')>0 AND substr(e.actor,instr(e.actor,'/'))=substr(l.owner,instr(l.owner,'/'))",
            (now,))}
        workers = []
        for goal, (owner, _) in planners.items():
            # Older runs queued same-CLI workers only after their tasks were created.
            queued = con.execute(
                "SELECT actor FROM events WHERE op='agent-queued' "
                "AND (node_id=? OR node_id IN (SELECT to_id FROM edges WHERE from_id=? AND kind='requires')) "
                "AND substr(actor,instr(actor,'/'))=? GROUP BY actor ORDER BY min(cursor)",
                (goal, goal, owner[owner.index("/"):]))
            workers.extend(row["actor"] for row in queued)

        # PRAGMA does not start a transaction; close it explicitly so nothing is held.
        con.execute("COMMIT")
    except sqlite3.Error as e:
        raise SpaceError(f"read failed: {e}") from e
    finally:
        con.close()

    known = set(nodes)
    edges = [e for e in edges if e["from_id"] in known and e["to_id"] in known]

    return {"nodes": nodes, "edges": edges, "leases": leases, "activity": activity,
            "planners": planners, "workers": workers, "now": now}


def status_of(n: dict) -> str:
    if n["kind"] == "fact":
        return n["fst"] or "UNVERIFIED"
    if n["kind"] == "task":
        return n["st"] or "OPEN"
    if n["kind"] == "decision":
        return "ACTIVE"
    if n["kind"] == "artifact":
        return "PRESENT"
    return "FAILED"


def derive_taint(nodes: dict, edges: list) -> set:
    """Recompute taint exactly as the CLI's refresh_taint() would.

    The `nodes.tainted` column is a cache only the CLI writes; a read-only viewer that
    trusted it would show stale taint forever. Roots are assumed-but-unverified facts
    (either flagged assumed, or targeted by an `assumes` edge); taint flows from a
    prerequisite to its dependents and the roots themselves are excluded.
    """
    assumed = {nid for nid, n in nodes.items()
               if n["kind"] == "fact" and n["assumed"] and status_of(n) != "VERIFIED"}
    assumed |= {e["to_id"] for e in edges
                if e["kind"] == "assumes"
                and status_of(nodes.get(e["to_id"], {"kind": "", "fst": ""})) != "VERIFIED"
                and e["to_id"] in nodes}

    tainted: set = set()
    frontier = [a for a in assumed if a in nodes]
    while frontier:
        cur = frontier.pop()
        for e in edges:
            if e["to_id"] == cur and e["kind"] in ("requires", "depends_on", "cites", "assumes"):
                src = e["from_id"]
                if src in nodes and src not in tainted:
                    tainted.add(src)
                    frontier.append(src)
    return tainted - assumed


def live_leases(leases: list, now: int) -> list:
    return [l for l in leases if l["owner"] and l["expires_at"] and l["expires_at"] > now]


# ---------------------------------------------------------------- render


def render(d: dict, agents: list, cols: int, rows: int) -> str:
    nodes, edges = d["nodes"], d["edges"]
    now = d["now"]
    tainted = derive_taint(nodes, edges)

    # blocking: a dependent whose prerequisite is not VERIFIED (or is tainted)
    blocking: dict = {}
    for e in edges:
        if e["kind"] not in ("requires", "depends_on"):
            continue
        dst = nodes.get(e["to_id"])
        if not dst:
            continue
        if dst["kind"] == "fact" and (status_of(dst) != "VERIFIED" or e["to_id"] in tainted):
            blocking.setdefault(e["from_id"], []).append(e["to_id"])

    live = live_leases(d["leases"], now)
    planners = d.get("planners", {})
    workers = d.get("workers", [])
    labels = {owner: planner.partition("/")[0] + " (orch)" for owner, planner in planners.values()}
    labels.update({worker: worker.partition("/")[0] + " (worker)" for worker in workers})

    def pane_name(owner):
        if owner in labels:
            return owner
        harness = owner.partition("/")[0]
        return harness if owner not in agents and harness in agents else owner

    by_owner: dict = {}
    dispatched = {event["actor"] for event in d.get("activity", [])}
    for l in live:
        by_owner.setdefault(pane_name(l["owner"]), []).append(l)
        if l["owner"] in dispatched and l["owner"] not in labels and "/" in l["owner"]:
            labels[pane_name(l["owner"])] = l["owner"].partition("/")[0] + " (worker)"
    latest = {}
    for event in d.get("activity", []):
        planner = planners.get(event["node_id"])
        if planner and planner[1] == event["actor"] and event["op"] != "agent-queued":
            name = planner[0]
        else:
            name = pane_name(event["actor"])
        latest.setdefault(name, event["detail"] or "")

    # Space is tight on a standard terminal. Put the current team before idle roster slots.
    team_harnesses = {actor.partition("/")[0] for _, actor in planners.values()}
    team_harnesses.update(worker.partition("/")[0] for worker in workers)
    names = list(dict.fromkeys(
        [owner for owner, _ in planners.values()] + sorted(by_owner) + workers
        + [name for name in agents if name not in team_harnesses]))

    # ---- right column content
    leases_lines = []
    for l in sorted(live, key=lambda x: x["expires_at"]):
        left = l["expires_at"] - now
        mark = g("active_dot") if left >= 120 else "!"
        leases_lines.append(f" {mark} {l['resource']}  {l['owner']}  {span(left)} left")
    if not leases_lines:
        leases_lines.append("  none")

    order = {"REFUTED": 0, "CONTESTED": 1, "TAINTED": 2, "BLOCKED": 3,
             "UNVERIFIED": 4, "OPEN": 5}
    frontier = []
    for nid, n in nodes.items():
        if n["kind"] == "fact" and status_of(n) in ("UNVERIFIED", "UNCHECKABLE"):
            frontier.append((order["UNVERIFIED"], nid))
        elif n["kind"] == "task" and status_of(n) == "OPEN":
            frontier.append((order["OPEN"], nid))
    frontier.sort()
    frontier_lines = []
    for _, nid in frontier[:FRONTIER_CAP]:
        n = nodes[nid]
        if nid in tainted:
            mark, word = g("tainted"), "TAINTED"
        elif nid in blocking:
            mark, word = g("blocked"), "BLOCKED"
        else:
            mark = g("unverified") if n["kind"] == "fact" else g("open")
            word = status_of(n)
        frontier_lines.append(f" {mark} {nid} {word} {n['title']}")
    if len(frontier) > FRONTIER_CAP:
        frontier_lines.append(f"  ... {len(frontier) - FRONTIER_CAP} more")
    if not frontier_lines:
        frontier_lines.append("  nothing open")

    attention = []
    for nid, n in nodes.items():
        st = status_of(n)
        if nid in tainted:
            attention.append((order["TAINTED"], nid, "TAINTED"))
        elif st in ("REFUTED", "CONTESTED"):
            attention.append((order[st], nid, st))
        # BLOCKED is intentionally NOT repeated here: it is waiting on a prerequisite and
        # already appears in FRONTIER with its blocked mark. Listing it in both sections
        # made the same task look like two separate problems.
    attention.sort()
    attention_lines = []
    glyph_for = {"TAINTED": g("tainted"), "REFUTED": g("refuted"),
                 "CONTESTED": g("contested"), "BLOCKED": g("blocked")}
    for _, nid, word in attention[:ATTENTION_CAP]:
        attention_lines.append(f" {glyph_for[word]} {nid} {word} {nodes[nid]['title']}")
    if len(attention) > ATTENTION_CAP:
        attention_lines.append(f"  ... {len(attention) - ATTENTION_CAP} more")
    if not attention_lines:
        attention_lines.append("  nothing needs attention")

    # ---- canvas
    #
    # Paint into a fixed char matrix rather than concatenating strings. String
    # concatenation produced ragged frames (rows 42 to 100 columns wide) and there was no
    # place to put the vertical spine. A canvas makes every row exactly `cols` wide and
    # makes the rope a thing you can point at, not an emergent side effect.
    label_w = max([len(labels.get(n, n)) for n in names] + [8])
    box_w = max(22, min(label_w + 8, 34))
    rope_w = 17
    # The spine IS the column separator: one rule the ropes join, with one arrowhead into
    # the orchestrator. Drawing a separate spine and separator produced "│   │", which
    # reads as two unrelated things.
    spine = box_w + rope_w
    right_x = spine + 2
    header_rows = 1
    footer_rows = 2
    body_rows = max(3, rows - header_rows - footer_rows)

    canvas = [[" "] * cols for _ in range(rows)]

    def put(y: int, x: int, text: str) -> None:
        for i, ch in enumerate(text):
            if 0 <= y < rows and 0 <= x + i < cols:
                canvas[y][x + i] = ch

    # ---- left: agent blocks, starting below the header row
    inner_w = box_w - 2
    bot_s = g("corner_bl") + g("hbar") * inner_w + g("corner_br")

    rope_centers = []          # (row, active)
    y = header_rows
    for name in names:
        held = by_owner.get(name, [])
        # A block grows for a multi-lease agent but extra leases collapse into "+N"
        # rather than being dropped silently.
        lease_rows = max(1, min(len(held), 2))
        message = latest.get(name)
        inner_rows = lease_rows + bool(message)
        if y + inner_rows + 2 > header_rows + body_rows:
            break              # out of room; the header count still shows the truth
        title = clip(" " + labels.get(name, name) + " ", inner_w).ljust(inner_w, g("hbar"))
        put(y, 0, g("corner_tl") + title + g("corner_tr"))
        for k in range(lease_rows):
            if k < len(held):
                l = held[k]
                left = l["expires_at"] - now
                body = clip(f" {l['resource']} lease {span(left)}", inner_w)
            else:
                body = clip(f" +{len(held) - inner_rows} more", inner_w)
            put(y + 1 + k, 0, g("vbar") + body.ljust(inner_w) + g("vbar"))
        if not held:
            put(y + 1, 0, g("vbar") + " idle".ljust(inner_w) + g("vbar"))
        if message:
            line = message.splitlines()[0]
            if ASCII:
                line = line.encode("ascii", "replace").decode("ascii")
            put(y + 1 + lease_rows, 0, g("vbar") + clip(" " + line, inner_w).ljust(inner_w) + g("vbar"))
        rope_centers.append((y + 1, bool(held)))
        put(y + 1 + inner_rows, 0, bot_s)
        y += inner_rows + 3          # block + one blank line

    # ---- spine, then ropes on top of it
    #
    # The rule is the spine: one column that is both the visual tether and the divider
    # between the roster and the orchestrator. It is drawn FIRST so the rope junctions
    # drawn afterwards are not erased by it, and the arrow is placed at spine-1 so it can
    # never overwrite the rule itself.
    for yy in range(header_rows, header_rows + body_rows):
        put(yy, spine, g("vbar"))

    if rope_centers:
        first = rope_centers[0][0]
        for center, active in rope_centers:
            fill = g("hbar") if active else "."
            for xx in range(box_w + 1, spine):
                put(center, xx, fill)
            put(center, spine, g("tee"))
        # ONE arrowhead, on the first active rope so the eye lands on work. It sits
        # immediately left of the rule and the rule survives, because a hole in the
        # divider on the arrow row would leave the orchestrator column unbounded.
        arrow_row = next((c for c, a in rope_centers if a), first)
        # Arrow in the corridor, plain rule behind it: "────▶│" reads as "this rope feeds
        # the orchestrator". Leaving the tee there too produced "▶├", two glyphs fighting
        # over the same junction.
        put(arrow_row, spine - 1, g("arrow"))
        put(arrow_row, spine, g("vbar"))

    right_lines: list = []
    right_lines.append("ORCHESTRATOR")
    right_lines.append(g("hbar") * max(0, cols - right_x))
    right_lines.append("LEASES")
    right_lines.extend(leases_lines)
    right_lines.append("")
    right_lines.append("FRONTIER")
    right_lines.extend(frontier_lines)
    right_lines.append("")
    right_lines.append("ATTENTION")
    right_lines.extend(attention_lines)

    for i, line in enumerate(right_lines):
        yy = header_rows + i
        if yy >= header_rows + body_rows:
            break
        put(yy, right_x, line)

    # ---- header + footer
    header = (f"REALM UP  {g('vbar')} {len(nodes)} nodes  "
              f"{len(live)} live lease{'s' if len(live) != 1 else ''}  "
              f"{g('vbar')} q=quit r=refresh")
    put(0, 0, clip(header, cols))
    put(rows - 2, 0, g("hbar") * cols)
    put(rows - 1, 0, clip(" solid rope = holds a lease   dotted rope = idle   "
                          "! = expiring   B = blocked", cols))
    return "\n".join("".join(row) for row in canvas)


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    global ASCII
    ap = argparse.ArgumentParser(prog="realm up")
    ap.add_argument("--once", action="store_true", help="render one frame and exit")
    ap.add_argument("--ascii", action="store_true", help="pure-ASCII glyphs")
    ap.add_argument("--agents", default=os.environ.get("REALM_UP_AGENTS", DEFAULT_AGENTS))
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--cols", type=int)
    ap.add_argument("--rows", type=int)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2

    ASCII = bool(a.ascii) or (os.environ.get("NO_COLOR") is not None
                              and os.environ.get("REALM_UNICODE") != "1")

    agents = [s.strip() for s in a.agents.split(",") if s.strip()]
    cols = a.cols or int(os.environ.get("COLUMNS", 100))
    rows = a.rows or int(os.environ.get("LINES", 30))
    # Degrade rather than crash: a small terminal gets a small frame, not a traceback.
    cols = max(40, cols)
    rows = max(12, rows)

    if a.once:
        try:
            d = load(find_space())
        except SpaceError as e:
            print(f"realm up: {e}", file=sys.stderr)
            return 1
        print(render(d, agents, cols, rows))
        return 0

    try:
        import curses
    except ImportError:
        print("realm up: curses unavailable; use --once", file=sys.stderr)
        return 1

    try:
        sp = find_space()
    except SpaceError as e:
        print(f"realm up: {e}", file=sys.stderr)
        return 1

    def loop(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        last_err = None
        while True:
            h, w = stdscr.getmaxyx()
            try:
                d = load(sp)
                frame = render(d, agents, w, h)
                last_err = None
            except SpaceError as e:
                frame = clip(f"realm up: {e}", w)
                last_err = str(e)
            stdscr.erase()
            for i, line in enumerate(frame.split("\n")[: max(0, h - 1)]):
                try:
                    stdscr.addstr(i, 0, line[: w - 1])
                except curses.error:
                    pass
            stdscr.refresh()
            deadline = time.time() + a.poll
            while time.time() < deadline:
                k = stdscr.getch()
                if k in (ord("q"), 27):
                    return
                if k == ord("r"):
                    break
                time.sleep(0.05)
        _ = last_err

    curses.wrapper(loop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
