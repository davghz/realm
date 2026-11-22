"""realm ui — a live view of the space.

Read-only. Never writes. Safe to run while agents work.

Design rules (from the settled review):
  - Adjacency tree, not force-directed. Nodes keep position; only glyphs mutate.
  - Glyph AND spelled reason. Nothing depends on colour. Pipes losslessly to a file.
  - Delta-first header: "is it working / is it stuck / what changed for you".
  - Every live claim prints its own age, so staleness is shown rather than buried.
  - Truncation is never silent: always "... N more".
  - Quitting loses nothing; this is a lens over `state`, not a second source of truth.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


# ----------------------------------------------------------------- glyphs

EVENT_TAIL = 40   # how many events the UI loads; anything older is reported as partial

GLYPH = {
    "VERIFIED":   ("✓", "V"),
    "UNVERIFIED": ("?", "?"),
    "UNCHECKABLE": ("?", "?"),
    "REFUTED":    ("✗", "X"),
    "CONTESTED":  ("!", "!"),
    "OPEN":       ("·", "."),
    "DONE":       ("✓", "V"),
    "DROPPED":    ("-", "-"),
    "ACTIVE":     ("·", "."),
}


def glyph(status: str, ascii_only: bool) -> str:
    a, b = GLYPH.get(status, ("?", "?"))
    return b if ascii_only else a


def width(s: str) -> int:
    """Display width, counting the glyphs we use as 1 column."""
    return len(s)


ELLIPSIS = "…"
SEP = "·"
VBAR = "│"


def set_ascii_mode(on: bool) -> None:
    """Switch the clip ellipsis, separators and tree connectors to pure ASCII."""
    global ELLIPSIS, SEP, VBAR
    ELLIPSIS = "~" if on else "…"
    SEP = "|" if on else "·"
    VBAR = "|" if on else "│"


def clip(s: str, n: int) -> str:
    s = s.replace("\n", " ").replace("\t", " ")
    if len(s) <= n:
        return s
    return s[: max(0, n - len(ELLIPSIS))] + ELLIPSIS


def age(ts: int | None, now: int) -> str:
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


# ----------------------------------------------------------------- data

def space_dir() -> Path:
    env = os.environ.get("REALM_SPACE")
    if env:
        return Path(env)
    here = Path.cwd()
    for p in [here, *here.parents]:
        c = p / ".realm"
        if c.is_dir():
            return c
    sys.exit("realm ui: no space found. run `realm init`.")


def load(sp: Path) -> dict:
    db = sp / "space.db"
    if not db.exists():
        sys.exit("realm ui: space not initialised. run `realm init`.")
    # A read-only handle cannot create space.db-shm. After a writer checkpoints and
    # cleans up, the -shm is gone, so EVERY query on a mode=ro handle fails with
    # SQLITE_CANTOPEN (surfacing as "attempt to write a readonly database"). That is not a
    # corrupt database and must not be reported as one.
    #
    # Fallback: open READWRITE and immediately set query_only. query_only blocks
    # statement writes but is NOT a no-recovery guarantee, so a hot journal is checked
    # first — refusing to open is the only safe response to one.
    journal = Path(str(db) + "-journal")
    if journal.is_file() and journal.stat().st_size > 0:
        sys.exit(f"realm ui: {db.name} needs recovery ({journal.name} is "
                 f"{journal.stat().st_size} bytes); refusing to open it read-only")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        con.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error:
        try:
            con.close()
        except Exception:
            pass
        # Re-check: a journal can appear between the check above and this open.
        if journal.is_file() and journal.stat().st_size > 0:
            sys.exit(f"realm ui: {db.name} needs recovery; refusing to open it")
        try:
            con = sqlite3.connect(str(db), timeout=5.0)
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as e:
            # A WAL database needs -shm, and creating it needs a writable directory.
            # No reader can work around that, so say so instead of raising.
            if not os.access(db.parent, os.W_OK):
                sys.exit(f"realm ui: cannot read {db} because {db.parent} is not "
                         f"writable. SQLite needs to create the -shm sidecar to read a "
                         f"WAL database. Restore write permission on the directory.")
            sys.exit(f"realm ui: cannot read {db}: {e}")
    con.row_factory = sqlite3.Row
    now = int(time.time())

    # One deferred read transaction gives a single consistent snapshot across all four
    # queries. Without it a WAL writer can commit a fact and its assumes-edge between the
    # node read and the edge read, producing edges that reference a node we never saw.
    con.execute("BEGIN")

    nodes = {}
    for r in con.execute(
            "SELECT n.id,n.kind,n.title,n.tainted,n.created_at,n.updated_at,"
            "       COALESCE(t.state,'') st, COALESCE(t.priority,50) prio,"
            "       COALESCE(f.status,'') fst, COALESCE(f.check_strength,'') fstr,"
            "       COALESCE(f.assumed,0) assumed, COALESCE(f.checked_at,NULL) checked"
            "  FROM nodes n"
            "  LEFT JOIN tasks t ON t.node_id=n.id"
            "  LEFT JOIN facts f ON f.node_id=n.id"):
        nodes[r["id"]] = dict(r)

    edges = [dict(r) for r in con.execute("SELECT from_id,to_id,kind FROM edges")]
    leases = [dict(r) for r in con.execute("SELECT * FROM leases")]
    ev = [dict(r) for r in con.execute(
        "SELECT cursor,at,actor,op,node_id,detail FROM events "
        f"ORDER BY cursor DESC LIMIT {EVENT_TAIL}")]
    cur = con.execute("SELECT COALESCE(MAX(cursor),0) c FROM events").fetchone()["c"]
    con.execute("COMMIT")
    con.close()

    # Defensive: an edge may reference a node absent from the snapshot (schema allows it
    # transiently). Drop such edges rather than crash later.
    known = set(nodes)
    edges = [e for e in edges if e["from_id"] in known and e["to_id"] in known]

    init = None
    try:
        init = json.loads((sp / "ui-opened.json").read_text()).get("cursor")
    except Exception:
        init = None

    return {"nodes": nodes, "edges": edges, "leases": leases,
            "events": ev, "cursor": cur, "since_cursor": init, "now": now,
            "event_limit": EVENT_TAIL}


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


def lease_for(leases, nid):
    for l in leases:
        if l["resource"] == nid and l["expires_at"] and l["expires_at"] > int(time.time()):
            return l
    return None


# ----------------------------------------------------------------- render

def render(d: dict, cols: int, rows: int, ascii_only: bool, since_mode: bool) -> str:
    nodes, edges, leases, ev = d["nodes"], d["edges"], d["leases"], d["events"]
    now, cur = d["now"], d["cursor"]

    # Taint is DERIVED, never trusted from the cached column: the UI is read-only and
    # cannot call refresh_taint(), so recompute the same closure the CLI uses.
    assumed = {nid for nid, n in nodes.items()
               if n["kind"] == "fact" and n.get("assumed") and status_of(n) != "VERIFIED"}
    assumed |= {e["to_id"] for e in edges if e["kind"] == "assumes"
                and status_of(nodes.get(e["to_id"], {})) != "VERIFIED"}
    tainted: set[str] = set()
    frontier = [a for a in assumed if a in nodes]
    while frontier:
        cur_id = frontier.pop()
        for e in edges:
            if e["to_id"] == cur_id and e["kind"] in ("requires", "depends_on", "cites", "assumes"):
                if e["from_id"] in nodes and e["from_id"] not in tainted:
                    tainted.add(e["from_id"])
                    frontier.append(e["from_id"])
    tainted -= assumed
    blocking = {}          # node -> list of prerequisite ids not VERIFIED
    for e in edges:
        if e["kind"] not in ("requires", "depends_on"):
            continue
        src, dst = nodes.get(e["from_id"]), nodes.get(e["to_id"])
        if not src or not dst:
            continue
        if dst["kind"] == "fact" and (status_of(dst) != "VERIFIED" or e["to_id"] in tainted):
            blocking.setdefault(e["from_id"], []).append(e["to_id"])

    live = [l for l in leases if l["expires_at"] and l["expires_at"] > now]
    stale = [l for l in leases if l["expires_at"] and l["expires_at"] <= now]

    counts = {}
    for n in nodes.values():
        s = status_of(n)
        if n["kind"] in ("fact", "task"):
            counts[s] = counts.get(s, 0) + 1

    # ---- header: three lines that answer the five-second questions ----
    last = ev[0] if ev else None
    # count only verifications that actually succeeded; a REFUTED replay is not motion
    motion = sum(1 for e in ev
                 if e["op"] == "verify" and now - e["at"] < 600
                 and (e.get("detail") or "").startswith("VERIFIED"))
    out = []
    inv = " ".join(f"{glyph(k, ascii_only)}{v}" for k, v in sorted(counts.items()) if v)
    out.append(clip(f"REALM {SEP} {len(nodes)} nodes {inv} {SEP} {len(live)} live leases", cols))
    watch = ""
    if live:
        oldest = min(live, key=lambda l: l["expires_at"])
        left = max(0, oldest["expires_at"] - now)
        watch = f" {SEP} watch: {oldest['resource']} expires in {left // 60}m"
    out.append(clip(f"motion: +{motion} verified in 10m {SEP} last event "
                    f"{age(last['at'], now) if last else '-'} ago{watch}", cols))
    if since_mode:
        cut = d["since_cursor"]
        limit = d.get("event_limit", 40)
        oldest = min((e["cursor"] for e in ev), default=None)

        # The event list is a TAIL. If the watermark predates the oldest row we hold, an
        # exact count is impossible: events between `cut` and `oldest` were never loaded.
        # Saying "N changed" there is an undercount presented as a total, which is a lie.
        truncated = oldest is not None and cut is not None and oldest > cut + 1
        if cut is None:
            ch = list(ev)
            truncated = len(ev) >= limit
        else:
            ch = [e for e in ev if e["cursor"] > cut]

        if truncated:
            note = f" (partial: tail {len(ev)} of a longer history)"
            count = f"{len(ch)}+"
        else:
            note = ""
            count = str(len(ch))

        out.append(clip(f"since {cut if cut is not None else 'open'}: {count} changed{note} "
                        f"{SEP} {', '.join(e['op'] for e in ch[:6]) or 'nothing'}", cols))
    else:
        out.append(clip(f"seq={cur} {SEP} s=since view {SEP} q=quit", cols))

    body_rows = rows - len(out) - 2
    # keep the joined line within `cols` even at very small widths
    left_w = max(12, min(int(cols * 0.62), max(12, cols - 14)))
    right_w = max(8, cols - left_w - 1)

    # ---- left: adjacency tree, severity-sorted ----
    order_rank = {"REFUTED": 0, "CONTESTED": 1, "UNCHECKABLE": 2, "UNVERIFIED": 3,
                  "OPEN": 4, "VERIFIED": 5, "DONE": 6, "DROPPED": 7}
    # a node is a root if it has no PREREQUISITE (no outgoing requires/depends_on)
    roots = [nid for nid, n in nodes.items()
             if not any(e["kind"] in ("requires", "depends_on")
                        and e["from_id"] == nid for e in edges)]
    roots.sort(key=lambda nid: (order_rank.get(status_of(nodes[nid]), 9),
                                -nodes[nid]["prio"], nodes[nid]["id"]))

    # prerequisite -> its dependents, so the tree reads "this, and what relies on it"
    children: dict[str, list[str]] = {}
    for e in edges:
        if e["kind"] in ("requires", "depends_on", "cites", "assumes"):
            children.setdefault(e["to_id"], []).append(e["from_id"])

    tree: list[str] = []
    seen: set[str] = set()

    def walk(nid: str, prefix: str, last_child: bool, depth: int):
        if nid in seen or depth > 3:
            return
        seen.add(nid)
        n = nodes[nid]
        st = status_of(n)
        if ascii_only:
            conn = "\\- " if last_child else "|- " if depth else ""
        else:
            conn = "└─ " if last_child else "├─ " if depth else ""
        g = glyph(st, ascii_only)
        marks = []
        if nid in tainted:
            marks.append("TAINTED")
        if nid in blocking:
            arrow = "<-" if ascii_only else "←"
            marks.append("BLOCKED" + arrow + ",".join(blocking[nid][:2]))
        if lease_for(leases, nid):
            marks.append("LEASED")
        if n.get("fstr") == "TRIVIAL":
            marks.append("trivial")
        # reserve room for marks FIRST, then fit the claim into what is left
        # LEASED first: it is the most operationally urgent mark
        if "LEASED" in marks:
            marks.remove("LEASED")
            marks.insert(0, "LEASED")
        mtext = (" " + " ".join(marks)) if marks else ""
        fixed = len(prefix) + len(conn) + 2 + 5 + 1 + 9 + 1
        room = max(10, left_w - fixed - len(mtext) - 1)
        claim = n["title"]
        if st == "REFUTED":
            claim = "REFUTED " + claim
        line = f"{prefix}{conn}{g} {nid:5s} {n['kind']:9s} {clip(claim, room)}{mtext}"
        tree.append(clip(line, left_w))
        kids = children.get(nid, [])
        for i, k in enumerate(kids):
            walk(k, prefix + ("   " if last_child else ("   " if ascii_only else "│  ")),
                 i == len(kids) - 1, depth + 1)

    for r in roots:
        walk(r, "", True, 0)
    for nid in nodes:
        if nid not in seen:
            walk(nid, "", True, 0)

    overflow = len(tree) > body_rows
    shown = tree[: max(1, body_rows - 1)] if overflow else tree[:body_rows]
    if overflow:
        shown.append(clip(f"  {ELLIPSIS} {len(tree) - len(shown)} more (f=full, / =filter)",
                          left_w))

    # ---- right: roster + detail + evidence tail ----
    agents: dict[str, dict] = {}
    for l in live:
        a = agents.setdefault(l["owner"], {"lease": [], "last": None})
        a["lease"].append(l["resource"])
    for e in ev:
        if e["actor"] not in agents:
            agents[e["actor"]] = {"lease": [], "last": None}
        if agents[e["actor"]]["last"] is None:
            agents[e["actor"]]["last"] = e

    right: list[str] = ["AGENTS"]
    for name in sorted(agents):
        a = agents[name]
        lea = ",".join(a["lease"]) or ("-" if ascii_only else "—")
        lg = a["last"]
        dot = "*" if lea != "—" else ("." if ascii_only else "·")
        right.append(clip(f" {dot} {name[:12]:12s} {lea[:8]:8s} "
                          f"{age(lg['at'], now) if lg else '-'}", right_w))
    right.append("")
    right.append("EVIDENCE")
    for e in ev[: max(3, body_rows - len(right) - 1)]:
        right.append(clip(f" {time.strftime('%H:%M', time.localtime(e['at']))} "
                          f"{e['op'][:10]:10s} {(e['node_id'] or ''):5s} "
                          f"{e['actor'][:8]}", right_w))

    pad = max(0, body_rows)
    while len(shown) < pad:
        shown.append("")
    while len(right) < pad:
        right.append("")

    zipped = [clip(f"{shown[i]:<{left_w}}{VBAR}{right[i]}", cols)
              for i in range(min(len(shown), len(right)))]
    out.extend(zipped)
    out.append(("-" if ascii_only else "─") * min(cols, max(0, cols - 1)))
    out.append(clip("enter=detail  s=since  f=full  /=filter  q=quit", cols))
    return "\n".join(out[:rows])


# ----------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="realm ui")
    ap.add_argument("--once", action="store_true", help="render one frame and exit")
    ap.add_argument("--since", action="store_true", help="open in delta view")
    ap.add_argument("--ascii", action="store_true", help="ASCII glyphs only")
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--cols", type=int); ap.add_argument("--rows", type=int)
    a = ap.parse_args(argv)

    sp = space_dir()
    cols = a.cols or int(os.environ.get("COLUMNS", 100))
    rows = a.rows or int(os.environ.get("LINES", 30))
    # explicit --ascii wins; otherwise use unicode unless the environment asks for plain
    ascii_only = bool(a.ascii) or (os.environ.get("NO_COLOR") is not None
                                   and os.environ.get("REALM_UNICODE") != "1")
    set_ascii_mode(ascii_only)

    if a.once:
        print(render(load(sp), cols, rows, ascii_only, a.since))
        return 0

    # read-only lens: hold the opening cursor in memory, never write to the space
    first = load(sp)

    import curses
    read_only = True  # this UI never writes to the space

    def loop(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        since_mode = a.since
        while True:
            h, w = stdscr.getmaxyx()
            d = load(sp)
            d["since_cursor"] = first["cursor"]
            frame = render(d, w, h, ascii_only, since_mode)
            stdscr.erase()
            stdscr.addstr(0, 0, frame[: (h - 1) * w])
            stdscr.refresh()
            deadline = time.time() + a.poll
            while time.time() < deadline:
                k = stdscr.getch()
                if k in (ord("q"), 27):
                    return
                if k == ord("s"):
                    since_mode = not since_mode
                    break
                time.sleep(0.05)

    curses.wrapper(loop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
