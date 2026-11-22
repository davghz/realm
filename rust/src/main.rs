//! realm — a shared space for agents to work together.
//!
//! Rust port of the Python reference implementation. Same schema, same exit codes,
//! same gates. The canonical 24-case suite runs against this binary unchanged.
//!
//! The point of the port is not speed. It is that the two defects which cost this
//! project the most were BOTH type errors Python could not see: a column referenced
//! in code but absent from the schema, and a swapped INSERT column order that a fk
//! constraint swallowed. Here the schema is a typed struct and the column order is
//! a compile-time fact.

use rusqlite::{params, Connection, OptionalExtension};
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

// ---------------------------------------------------------------- exit codes

/// Must match `PRAGMA user_version` in schema.sql.
const SCHEMA_VERSION: i64 = 2;

const OK: i32 = 0;
const USAGE: i32 = 1;
const REFUSED: i32 = 2;
const MISSING: i32 = 3;
const LEASE: i32 = 4;
const VFAIL: i32 = 5;
const UNCHECK: i32 = 8;

const DEFAULT_TTL: i64 = 900;
const TRIVIAL_MS: u128 = 50;

// ---------------------------------------------------------------- plumbing

fn die(code: i32, msg: &str) -> ! {
    eprintln!("{msg}");
    std::process::exit(code)
}

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn actor() -> String {
    std::env::var("REALM_AGENT")
        .or_else(|_| std::env::var("AGENT_NAME"))
        .unwrap_or_else(|_| "unknown".to_string())
}

fn space_dir() -> PathBuf {
    if let Ok(p) = std::env::var("REALM_SPACE") {
        return PathBuf::from(p);
    }
    let mut cur = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    loop {
        let cand = cur.join(".realm");
        if cand.is_dir() {
            return cand;
        }
        if !cur.pop() {
            die(USAGE, "realm: no space found. run `realm init`.");
        }
    }
}

fn open() -> Connection {
    let p = space_dir().join("space.db");
    if !p.exists() {
        die(USAGE, "realm: space not initialised. run `realm init`.");
    }
    let c = Connection::open(&p).unwrap_or_else(|e| die(7, &format!("realm: {e}")));
    c.busy_timeout(std::time::Duration::from_secs(10)).ok();
    c.pragma_update(None, "foreign_keys", "ON").ok();

    // Refuse a schema this build does not understand.
    //
    // The Python CLI did not check this either, and the two diverged: Python refused a
    // schema-99 space while Rust read it happily. Divergence between the reference and
    // the port is the failure this port exists to catch, so both check, worded the same.
    let ver: i64 = c
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap_or_else(|e| die(7, &format!("realm: cannot read schema version: {e}")));
    if ver != SCHEMA_VERSION {
        let msg = if ver == 0 {
            "realm: this file predates schema versioning and cannot be used safely. Re-create the space with `realm init`.".to_string()
        } else if ver > SCHEMA_VERSION {
            format!("realm: this space was made by a newer realm (schema {ver}); this build understands {SCHEMA_VERSION}. Update realm first.")
        } else {
            format!("realm: schema {ver} is older than {SCHEMA_VERSION}. Run the migration, or re-create the space.")
        };
        die(USAGE, &msg);
    }
    c
}

/// BEGIN IMMEDIATE, always.
///
/// A DEFERRED transaction only takes the write lock on its first write. If another agent
/// holds that lock at that moment, SQLite returns SQLITE_BUSY immediately instead of
/// honouring busy_timeout, so a read-then-write command fails with exit 7 while Python
/// simply waits. IMMEDIATE acquires the lock up front and waits properly.
fn txn<T>(c: &mut Connection, f: impl FnOnce(&rusqlite::Transaction) -> Result<T, String>) -> T {
    use rusqlite::TransactionBehavior;
    let t = c.transaction_with_behavior(TransactionBehavior::Immediate)
        .unwrap_or_else(|e| die(7, &format!("realm: begin: {e}")));
    match f(&t) {
        Ok(v) => {
            t.commit().unwrap_or_else(|e| die(7, &format!("realm: commit: {e}")));
            v
        }
        Err(e) => {
            let _ = t.rollback();
            die(7, &format!("realm: {e}"))
        }
    }
}

/// Append an event. Errors PROPAGATE: a swallowed INSERT here previously let a mutation
/// commit with no audit row, which is worse than failing outright.
fn emit(t: &rusqlite::Transaction, op: &str, node: Option<&str>, detail: Option<&str>)
        -> Result<(), String> {
    t.execute(
        "INSERT INTO events(at,actor,op,node_id,detail) VALUES (?1,?2,?3,?4,?5)",
        params![now(), actor(), op, node, detail],
    ).map(|_| ()).map_err(|e| format!("event insert failed: {e}"))
}

fn node_exists(c: &Connection, id: &str) -> Option<(String, String)> {
    c.query_row("SELECT kind,title FROM nodes WHERE id=?1", params![id],
                |r| Ok((r.get(0)?, r.get(1)?))).optional().ok().flatten()
}

fn next_id(c: &Connection, kind: &str) -> String {
    let pre = match kind {
        "task" => "T", "fact" => "F", "decision" => "D",
        "artifact" => "A", _ => "X",
    };
    let n: i64 = c
        .query_row(
            "SELECT COALESCE(MAX(CAST(SUBSTR(id,2) AS INTEGER)),0) FROM nodes WHERE kind=?1",
            params![kind], |r| r.get(0))
        .unwrap_or(0);
    format!("{}{:03}", pre, n + 1)
}

// ---------------------------------------------------------------- taint

/// Roots: facts explicitly assumed, or named by an `assumes` edge, that are not VERIFIED.
fn assumed_roots(c: &Connection) -> Vec<String> {
    let mut out: BTreeSet<String> = BTreeSet::new();
    if let Ok(mut s) = c.prepare(
        "SELECT node_id FROM facts WHERE assumed=1 AND status<>'VERIFIED'") {
        if let Ok(rows) = s.query_map([], |r| r.get::<_, String>(0)) {
            for r in rows.flatten() { out.insert(r); }
        }
    }
    if let Ok(mut s) = c.prepare(
        "SELECT e.to_id FROM edges e JOIN facts f ON f.node_id=e.to_id
         WHERE e.kind='assumes' AND f.status<>'VERIFIED'") {
        if let Ok(rows) = s.query_map([], |r| r.get::<_, String>(0)) {
            for r in rows.flatten() { out.insert(r); }
        }
    }
    out.into_iter().collect()
}

/// Transitive dependents. Edges point dependent -> prerequisite, so walk to_id -> from_id.
fn tainted_set(c: &Connection) -> BTreeSet<String> {
    let roots = assumed_roots(c);
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut frontier = roots.clone();
    while let Some(cur) = frontier.pop() {
        if let Ok(mut s) = c.prepare(
            "SELECT from_id FROM edges WHERE to_id=?1
             AND kind IN ('requires','depends_on','cites','assumes')") {
            if let Ok(rows) = s.query_map(params![cur], |r| r.get::<_, String>(0)) {
                for r in rows.flatten() {
                    if seen.insert(r.clone()) { frontier.push(r); }
                }
            }
        }
    }
    for r in roots { seen.remove(&r); }
    seen
}

fn refresh_taint(t: &rusqlite::Transaction) -> Result<BTreeSet<String>, String> {
    let s = tainted_set_t(t);
    t.execute("UPDATE nodes SET tainted=0 WHERE tainted=1", [])
        .map_err(|e| format!("taint reset failed: {e}"))?;
    for id in &s {
        t.execute("UPDATE nodes SET tainted=1 WHERE id=?1", params![id])
            .map_err(|e| format!("taint mark failed: {e}"))?;
    }
    Ok(s)
}

fn tainted_set_t(t: &rusqlite::Transaction) -> BTreeSet<String> {
    let roots = assumed_roots_t(t);
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut frontier = roots.clone();
    while let Some(cur) = frontier.pop() {
        if let Ok(mut s) = t.prepare(
            "SELECT from_id FROM edges WHERE to_id=?1
             AND kind IN ('requires','depends_on','cites','assumes')") {
            if let Ok(rows) = s.query_map(params![cur], |r| r.get::<_, String>(0)) {
                for r in rows.flatten() {
                    if seen.insert(r.clone()) { frontier.push(r); }
                }
            }
        }
    }
    for r in roots { seen.remove(&r); }
    seen
}

fn assumed_roots_t(t: &rusqlite::Transaction) -> Vec<String> {
    let mut out: BTreeSet<String> = BTreeSet::new();
    {
        let mut s = t.prepare("SELECT node_id FROM facts WHERE assumed=1 AND status<>'VERIFIED'").unwrap();
        let rows = s.query_map([], |r| r.get::<_, String>(0)).unwrap();
        for r in rows.flatten() { out.insert(r); }
    }
    {
        let mut s = t.prepare(
            "SELECT e.to_id FROM edges e JOIN facts f ON f.node_id=e.to_id
             WHERE e.kind='assumes' AND f.status<>'VERIFIED'").unwrap();
        let rows = s.query_map([], |r| r.get::<_, String>(0)).unwrap();
        for r in rows.flatten() { out.insert(r); }
    }
    out.into_iter().collect()
}

// ---------------------------------------------------------------- checks

fn is_trivial_cmd(cmd: &str) -> bool {
    matches!(cmd.trim().to_lowercase().as_str(),
             "true" | ":" | "exit 0" | "/bin/true" | "pass" | "ok" | "echo")
}

/// Returns (exit_code, output, elapsed_ms, trivial).
///
/// Pipes are drained CONCURRENTLY with the wait. Waiting first and reading after deadlocks
/// as soon as the child writes more than the pipe buffer (~64 KiB), which is exactly what a
/// verbose check does.
fn run_check(cmd: &str, timeout_secs: u64) -> (i32, String, u128, bool) {
    if is_trivial_cmd(cmd) {
        return (0, "(trivial check)".into(), 0, true);
    }
    let project = space_dir().parent().map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    let t0 = std::time::Instant::now();
    let spawned = Command::new("/bin/sh")
        .arg("-c").arg(cmd)
        .current_dir(&project)
        .env_clear()
        .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        .env("HOME", &project)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();
    let mut child = match spawned {
        Ok(c) => c,
        Err(e) => return (127, format!("(spawn failed: {e})"), 0, false),
    };

    // Drain both pipes on their own threads so the child is never blocked on a full pipe.
    let mut out_pipe = child.stdout.take();
    let mut err_pipe = child.stderr.take();
    let out_handle = std::thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(p) = out_pipe.as_mut() {
            use std::io::Read;
            let _ = p.read_to_end(&mut buf);
        }
        buf
    });
    let err_handle = std::thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(p) = err_pipe.as_mut() {
            use std::io::Read;
            let _ = p.read_to_end(&mut buf);
        }
        buf
    });

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
    let mut timed_out = false;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => {
                if std::time::Instant::now() > deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    timed_out = true;
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            Err(_) => break,
        }
    }

    let stdout = out_handle.join().unwrap_or_default();
    let stderr = err_handle.join().unwrap_or_default();
    let ms = t0.elapsed().as_millis();
    if timed_out {
        return (124, "(timeout)".into(), ms, false);
    }
    let code = child.wait().ok().and_then(|s| s.code()).unwrap_or(1);
    let mut text = String::from_utf8_lossy(&stdout).to_string();
    text.push_str(&String::from_utf8_lossy(&stderr));
    let text: String = text.chars().take(2000).collect();
    let trivial = code == 0 && ms < TRIVIAL_MS && text.trim().is_empty();
    (code, text, ms, trivial)
}

fn sha16(s: &str) -> String {
    // deliberately simple: FNV-1a 64-bit rendered hex. Not cryptographic; it identifies a
    // check descriptor for caching, and the canonical suite does not depend on the algorithm.
    let mut h: u64 = 0xcbf29ce484222325;
    for b in s.as_bytes() {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{h:016x}")
}

// ---------------------------------------------------------------- commands

fn cmd_init(path: Option<&str>) -> i32 {
    let root = PathBuf::from(path.unwrap_or("."));
    let sp = root.join(".realm");
    if let Err(e) = std::fs::create_dir_all(sp.join("tmp")) {
        die(7, &format!("realm: {e}"));
    }
    let sp_path = schema_path().unwrap_or_else(|| {
        die(7, "realm: schema.sql not found (set REALM_SCHEMA or run from the project root)")
    });
    let schema = std::fs::read_to_string(&sp_path).unwrap_or_else(|e| {
        die(7, &format!("realm: {}: {e}", sp_path.display()))
    });
    let c = Connection::open(sp.join("space.db")).unwrap_or_else(|e| die(7, &format!("realm: {e}")));
    if let Err(e) = c.execute_batch(&schema) {
        die(7, &format!("realm: schema: {e}"));
    }
    println!("realm: initialised {}", sp.display());
    OK
}

/// Locate schema.sql. Order matters:
///   1. REALM_SCHEMA (explicit override)
///   2. beside the binary (installed layout)
///   3. the project root implied by REALM_SPACE or the found .realm dir
///   4. the current directory
/// A missing schema is a hard error, never a silent empty database.
fn schema_path() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("REALM_SCHEMA") {
        let pb = PathBuf::from(p);
        if pb.exists() { return Some(pb); }
    }
    let exe = std::env::current_exe().ok()?;
    let mut dir = exe.parent()?.to_path_buf();
    for _ in 0..4 {
        let cand = dir.join("schema.sql");
        if cand.exists() { return Some(cand); }
        if !dir.pop() { break; }
    }
    // relative to the space: .realm/../schema.sql
    if let Ok(sp) = std::env::var("REALM_SPACE") {
        let cand = PathBuf::from(sp).parent().map(|p| p.join("schema.sql"));
        if let Some(c) = cand { if c.exists() { return Some(c); } }
    }
    let c = PathBuf::from("schema.sql");
    if c.exists() { return Some(c); }
    None
}

struct AddArgs {
    kind: String,
    title: String,
    id: Option<String>,
    body: Option<String>,
    check: Option<String>,
    path: Option<String>,
    priority: i64,
    cost: i64,
    attempt_limit: i64,
    depends_on: Vec<String>,
}

fn cmd_add(a: AddArgs) -> i32 {
    let mut c = open();
    let out = txn(&mut c, |t| {
        let id = a.id.clone().unwrap_or_else(|| next_id(t, &a.kind));
        let ts = now();
        t.execute(
            "INSERT INTO nodes(id,kind,title,body,created_by,created_at,updated_at)
             VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![id, a.kind, a.title, a.body.clone().unwrap_or_default(), actor(), ts, ts],
        ).map_err(|e| e.to_string())?;

        match a.kind.as_str() {
            "task" => {
                t.execute("INSERT INTO tasks(node_id,state,priority,cost) VALUES (?1,'OPEN',?2,?3)",
                          params![id, a.priority, a.cost]).map_err(|e| e.to_string())?;
            }
            "fact" => {
                let ch = a.check.as_ref().map(|c| sha16(c));
                let status = if a.check.is_some() { "UNVERIFIED" } else { "UNCHECKABLE" };
                t.execute("INSERT INTO facts(node_id,claim,status,check_cmd,check_hash,attempt_limit)
                           VALUES (?1,?2,?3,?4,?5,?6)",
                          params![id, a.title, status, a.check, ch, a.attempt_limit])
                    .map_err(|e| e.to_string())?;
            }
            "decision" => {
                t.execute("INSERT INTO decisions(node_id,rationale) VALUES (?1,?2)",
                          params![id, a.body.clone().unwrap_or_default()]).map_err(|e| e.to_string())?;
            }
            "artifact" => {
                let p = a.path.clone().unwrap_or_else(|| die(USAGE, "realm: artifact needs --path"));
                let bytes = std::fs::read(&p).unwrap_or_else(|e| die(USAGE, &format!("realm: {e}")));
                let digest = sha256_hex(&bytes);
                t.execute("INSERT INTO artifacts(node_id,digest,byte_count,source_path)
                           VALUES (?1,?2,?3,?4)",
                          params![id, digest, bytes.len() as i64, p]).map_err(|e| e.to_string())?;
            }
            _ => {
                t.execute("INSERT INTO attempts(node_id,run_id,run_kind,argv_json,cwd,started_at)
                           VALUES (?1,?2,'run',?3,?4,?5)",
                          params![id, id, format!("[\"{}\"]", a.title),
                                  std::env::current_dir().map(|p| p.display().to_string()).unwrap_or_default(),
                                  ts]).map_err(|e| e.to_string())?;
            }
        }

        for dep in &a.depends_on {
            if node_exists(t, dep).is_none() { die(MISSING, &format!("realm: no such node {dep}")); }
            t.execute("INSERT INTO edges(from_id,to_id,kind,at) VALUES (?1,?2,'requires',?3)
                       ON CONFLICT DO NOTHING", params![id, dep, ts])
                .map_err(|e| format!("dependency edge for {dep} rejected: {e}"))?;
        }
        emit(t, "add", Some(&id), Some(&format!("{}:{}", a.kind, a.title)))?;
        Ok(id)
    });
    println!("{out}");
    OK
}

fn cmd_take(task: &str, ttl: Option<i64>) -> i32 {
    let t = match ttl { Some(v) if v < 0 => die(USAGE, "realm: --ttl must be >= 0"), _ => () };
    let _ = t;
    let ttl = ttl.unwrap_or(DEFAULT_TTL);
    let mut c = open();
    txn(&mut c, |tx| {
        match node_exists(tx, task) {
            Some((k, _)) if k == "task" => {}
            Some((k, _)) => die(USAGE, &format!("realm: {task} is a {k}, not a task")),
            None => die(MISSING, &format!("realm: no such node {task}")),
        }
        // a completed task cannot be re-leased
        let state: Option<String> = tx.query_row(
            "SELECT state FROM tasks WHERE node_id=?1", params![task],
            |r| r.get(0)).optional().ok().flatten();
        if state.as_deref() == Some("DONE") {
            eprintln!("realm: {task} is already DONE");
            return Ok(REFUSED);
        }
        let ts = now();
        let changed = tx.execute(
            "INSERT INTO leases(resource,owner,fencing_token,acquired_at,expires_at)
             VALUES (?1,?2,1,?3,?4)
             ON CONFLICT(resource) DO UPDATE SET
                owner=excluded.owner,
                fencing_token=leases.fencing_token+1,
                acquired_at=excluded.acquired_at,
                expires_at=excluded.expires_at
             WHERE leases.owner IS NULL OR leases.expires_at <= ?3",
            params![task, actor(), ts, ts + ttl],
        ).map_err(|e| e.to_string())?;
        if changed == 0 {
            let held: Option<(String, i64)> = tx.query_row(
                "SELECT owner,expires_at FROM leases WHERE resource=?1", params![task],
                |r| Ok((r.get(0)?, r.get(1)?))).optional().ok().flatten();
            if let Some((owner, exp)) = held {
                eprintln!("realm: {task} leased by {owner}");
                let _ = exp;
            }
            return Ok(LEASE);
        }
        let tok: i64 = tx.query_row("SELECT fencing_token FROM leases WHERE resource=?1",
                                    params![task], |r| r.get(0)).unwrap();
        emit(tx, "take", Some(task), Some(&format!("ttl={ttl}")))?;
        println!("{task} leased to {} token={tok} ttl={ttl}", actor());
        Ok(OK)
    })
}

fn cmd_verify(fact: &str, force: bool) -> i32 {
    let mut c = open();

    // Look up + cache-check + run the check + record, with the transaction taken as
    // IMMEDIATE before any read. A DEFERRED transaction that later upgrades to a write
    // fails immediately with SQLITE_BUSY when another writer holds the lock; IMMEDIATE
    // waits on busy_timeout instead, which is what Python does.
    // Distinguish "missing fact" (exit 3) from an internal error before taking the txn.
    {
        let known: Option<String> = c.query_row(
            "SELECT kind FROM nodes WHERE id=?1", params![fact],
            |r| r.get(0)).optional().ok().flatten();
        match known.as_deref() {
            Some("fact") => {}
            Some(_) => die(USAGE, &format!("realm: {fact} is not a fact")),
            None => die(MISSING, &format!("realm: no such node {fact}")),
        }
    }

    let outcome = txn(&mut c, |t| -> Result<(String, i32, u128, u32, String), String> {
        let row: Option<(String, Option<String>, Option<String>, i64)> = t.query_row(
            "SELECT status,check_cmd,check_hash,attempt_limit FROM facts WHERE node_id=?1",
            params![fact], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
            .optional().map_err(|e| e.to_string())?;
        let (_, check_cmd, check_hash, limit) = match row {
            Some(v) => v,
            None => return Err(format!("fact row vanished: {fact}")),
        };
        let cmd = match check_cmd {
            Some(v) => v,
            None => {
                t.execute("UPDATE facts SET status='UNCHECKABLE' WHERE node_id=?1", params![fact])
                    .map_err(|e| e.to_string())?;
                emit(t, "verify-uncheckable", Some(fact), None)?;
                return Ok(("UNCHECKABLE".into(), 0, 0, 0, String::new()));
            }
        };
        let chash = check_hash.clone().unwrap_or_default();
        let ihash = format!("{fact}:{}", sha16(&cmd));

        if !force {
            let cached: Option<(String, Option<String>)> = t.query_row(
                "SELECT result,strength FROM verify_cache
                  WHERE fact_id=?1 AND check_hash=?2 AND input_hash=?3",
                params![fact, chash, ihash],
                |r| Ok((r.get(0)?, r.get(1)?))).optional().map_err(|e| e.to_string())?;
            if let Some((result, strength)) = cached {
                let ok = result == "PASS";
                return Ok((result, if ok { 0 } else { 1 }, 0, 0,
                           strength.unwrap_or_else(|| "OK".into())));
            }
        }

        // previous success decides CONTESTED
        let prev_pass: bool = t.query_row(
            "SELECT EXISTS(SELECT 1 FROM events WHERE node_id=?1 AND op='verify'
                            AND detail LIKE 'VERIFIED%')",
            params![fact], |r| r.get::<_, i64>(0)).map_err(|e| e.to_string())? == 1;

        // run the check OUTSIDE the lock is ideal, but the transaction keeps the facts row
        // consistent; checks are short. (Long checks should use --ttl-style handling.)
        let mut attempts = 0u32;
        let mut code = 1i32;
        let mut ms = 0u128;
        let mut trivial = false;
        while (attempts as i64) < limit.max(1) {
            attempts += 1;
            let (c2, _out, m, tr) = run_check(&cmd, 60);
            code = c2; ms = m; trivial = tr;
            if code == 0 { break; }
        }
        let ok = code == 0;
        let strength = if trivial { "TRIVIAL" } else { "OK" };
        let status = if ok { "VERIFIED" } else if prev_pass { "CONTESTED" } else { "REFUTED" };

        t.execute("UPDATE facts SET status=?1,check_exit=?2,checked_at=?3,check_strength=?4,
                   rerun_after_taint=?5 WHERE node_id=?6",
                  params![status, code, now(), strength, if force {1} else {0}, fact])
            .map_err(|e| e.to_string())?;
        t.execute("UPDATE nodes SET updated_at=?1 WHERE id=?2", params![now(), fact])
            .map_err(|e| e.to_string())?;
        t.execute("INSERT INTO verify_cache(fact_id,check_hash,input_hash,result,attempt_count,
                    strength,updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7)
                    ON CONFLICT(fact_id,check_hash,input_hash) DO UPDATE SET
                      result=excluded.result, attempt_count=excluded.attempt_count,
                      strength=excluded.strength, updated_at=excluded.updated_at",
                  params![fact, chash, ihash, if ok { "PASS" } else { "FAIL" },
                          attempts as i64, strength, now()])
            .map_err(|e| e.to_string())?;
        emit(t, "verify", Some(fact), Some(&format!("{status} exit={code}")))?;
        Ok((status.to_string(), code, ms, attempts, strength.to_string()))
    });

    {
        let (status, code, ms, attempts, strength) = outcome;
        {
            if status == "UNCHECKABLE" {
                println!("{fact} UNCHECKABLE (no check registered)");
                return UNCHECK;
            }
            if strength == "TRIVIAL" {
                println!("{fact} {status} ({strength}: a trivial check proves nothing to others)");
            } else if ms == 0 {
                println!("{fact} {status} (cached, {strength})");
            } else {
                println!("{fact} {status} exit={code} {ms}ms attempts={attempts}");
            }
            if status == "VERIFIED" || status == "PASS" { OK }
            else if code == 0 { OK } else { VFAIL }
        }
    }
}

fn cmd_done(task: &str) -> i32 {
    let mut c = open();
    txn(&mut c, |t| {
        match node_exists(t, task) {
            Some((k, _)) if k == "task" => {}
            Some((k, _)) => die(USAGE, &format!("realm: {task} is a {k}, not a task")),
            None => die(MISSING, &format!("realm: no such node {task}")),
        }
        let lease: Option<(String, i64)> = t.query_row(
            "SELECT owner,expires_at FROM leases WHERE resource=?1", params![task],
            |r| Ok((r.get(0)?, r.get(1)?))).optional().ok().flatten();
        match lease {
            None => { eprintln!("realm: {task} is not leased to you (holder: none)"); return Ok(LEASE); }
            Some((owner, exp)) if owner != actor() => {
                eprintln!("realm: {task} is not leased to you (holder: {owner})");
                return Ok(LEASE);
            }
            Some((_, exp)) if exp <= now() => {
                eprintln!("realm: REFUSED — your lease on {task} expired. Re-take it.");
                return Ok(REFUSED);
            }
            _ => {}
        }

        let tainted = refresh_taint(t)?;
        let mut blocking: Vec<(String, String)> = Vec::new();
        {
            let mut s = t.prepare(
                "SELECT f.node_id,f.status FROM edges e JOIN facts f ON f.node_id=e.to_id
                 WHERE e.from_id=?1 AND e.kind IN ('requires','depends_on')").unwrap();
            let rows = s.query_map(params![task], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).unwrap();
            for (id, st) in rows.flatten() {
                let mut why = Vec::new();
                if st != "VERIFIED" { why.push(st.clone()); }
                if tainted.contains(&id) { why.push("tainted".to_string()); }
                if !why.is_empty() { blocking.push((id, why.join(","))); }
            }
        }
        if !blocking.is_empty() {
            eprintln!("realm: REFUSED — {} unresolved fact(s) block {task}:", blocking.len());
            for (id, why) in &blocking {
                eprintln!("    {id}  {why}");
                eprintln!("    fix: realm verify {id}");
            }
            emit(t, "done-refused", Some(task), Some("blocked"))?;
            return Ok(REFUSED);
        }

        // GATE 1's completion writes must be atomic. A trigger can reject the UPDATE
        // (e.g. BEFORE UPDATE OF state ON tasks ... RAISE(ABORT)); if that happened while
        // we still deleted the lease and wrote the event, the reported success would be a
        // lie: task OPEN, lease gone, done-event committed.
        let n = t.execute("UPDATE tasks SET state='DONE' WHERE node_id=?1", params![task])
            .map_err(|e| format!("task state update rejected: {e}"))?;
        if n == 0 {
            return Err(format!("no tasks row for {task}"));
        }
        t.execute("UPDATE nodes SET updated_at=?1 WHERE id=?2", params![now(), task])
            .map_err(|e| format!("node timestamp update failed: {e}"))?;
        t.execute("DELETE FROM leases WHERE resource=?1", params![task])
            .map_err(|e| format!("lease release failed: {e}"))?;
        emit(t, "done", Some(task), None)?;
        println!("{task} DONE");
        Ok(OK)
    })
}

fn cmd_cite(fact: &str, assume: bool, for_node: Option<&str>) -> i32 {
    let mut c = open();
    txn(&mut c, |t| {
        let (status, strength, author, claim): (String, Option<String>, String, String) = match t.query_row(
            "SELECT f.status,f.check_strength,n.created_by,f.claim
               FROM facts f JOIN nodes n ON n.id=f.node_id WHERE f.node_id=?1",
            params![fact], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).optional().ok().flatten() {
            Some(v) => v,
            None => die(MISSING, &format!("realm: no such fact {fact}")),
        };
        let trivial = strength.as_deref() == Some("TRIVIAL");

        if status == "VERIFIED" && !trivial {
            if let Some(f) = for_node {
                t.execute("INSERT INTO edges(from_id,to_id,kind,at) VALUES (?1,?2,'cites',?3)
                           ON CONFLICT DO NOTHING", params![f, fact, now()])
                    .map_err(|e| format!("cites edge rejected: {e}"))?;
            }
            emit(t, "cite", Some(fact), Some("ok"))?;
            let shown: String = claim.chars().take(60).collect();
            println!("cite ok: {fact} {shown}");
            return Ok(OK);
        }
        if status == "VERIFIED" && trivial {
            if actor() == author {
                println!("cite ok (warning): {fact} passed only a TRIVIAL check — it proves nothing to other agents");
                return Ok(OK);
            }
            eprintln!("realm: REFUSED — {fact} passed only a TRIVIAL check registered by {author}; \
                       it cannot be cited by anyone else.");
            eprintln!("    fix: register a check that can fail, then realm verify {fact}");
            return Ok(REFUSED);
        }
        if status == "CONTESTED" {
            eprintln!("realm: REFUSED — {fact} is CONTESTED: a replay failed after it passed.");
            eprintln!("    fix: realm verify {fact} --force");
            return Ok(REFUSED);
        }
        if status == "REFUTED" {
            eprintln!("realm: REFUSED — {fact} is REFUTED by its own check.");
            return Ok(REFUSED);
        }
        if assume {
            t.execute("UPDATE facts SET assumed=1 WHERE node_id=?1", params![fact])
                .map_err(|e| format!("assume flag update failed: {e}"))?;
            if let Some(f) = for_node {
                if node_exists(t, f).is_none() {
                    return Err(format!("no such node {f}"));
                }
                t.execute("INSERT INTO edges(from_id,to_id,kind,at) VALUES (?1,?2,'assumes',?3)
                           ON CONFLICT DO NOTHING", params![f, fact, now()])
                    .map_err(|e| format!("assumes edge rejected: {e}"))?;
            }
            emit(t, "cite-assume", Some(fact), Some(&status))?;
            println!("cite ASSUMED: {fact} — anything depending on it is now tainted.");
            return Ok(OK);
        }
        eprintln!("realm: REFUSED — {fact} is {status}.");
        eprintln!("    fix: realm verify {fact}   (this refusal is the system working)");
        Ok(REFUSED)
    })
}

fn cmd_link(src: &str, kind: &str, dst: &str) -> i32 {
    let mut c = open();
    txn(&mut c, |t| {
        if node_exists(t, src).is_none() { die(MISSING, &format!("realm: no such node {src}")); }
        if node_exists(t, dst).is_none() { die(MISSING, &format!("realm: no such node {dst}")); }
        // NOTE: column order is (from_id, to_id, kind, at). Do NOT swallow errors here:
        // a silent CHECK violation previously made every link look successful.
        t.execute("INSERT INTO edges(from_id,to_id,kind,at) VALUES (?1,?2,?3,?4)
                   ON CONFLICT DO NOTHING", params![src, dst, kind, now()])
            .map_err(|e| format!("edge rejected: {e}"))?;
        emit(t, "link", Some(src), Some(&format!("{kind}->{dst}")))?;
        println!("{src} {kind} {dst}");
        Ok(OK)
    })
}

fn cmd_state(budget: Option<usize>) -> i32 {
    let mut c = open();
    let tainted = {
        use rusqlite::TransactionBehavior;
        let t = c.transaction_with_behavior(TransactionBehavior::Immediate)
            .unwrap_or_else(|e| die(7, &format!("realm: begin: {e}")));
        let s = match refresh_taint(&t) {
            Ok(v) => v,
            Err(e) => {
                let _ = t.rollback();
                die(7, &format!("realm: {e}"))
            }
        };
        t.commit().unwrap_or_else(|e| die(7, &format!("realm: commit: {e}")));
        s
    };
    let ts = now();
    let cur: i64 = c.query_row("SELECT COALESCE(MAX(cursor),0) FROM events", [], |r| r.get(0)).unwrap_or(0);
    let mut live = Vec::new();
    {
        let mut s = c.prepare("SELECT resource,owner FROM leases WHERE expires_at > ?1").unwrap();
        let rows = s.query_map(params![ts], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).unwrap();
        for r in rows.flatten() { live.push(r); }
    }
    let stale: i64 = c.query_row("SELECT COUNT(*) FROM leases WHERE expires_at <= ?1", params![ts], |r| r.get(0)).unwrap_or(0);

    let mut frontier: Vec<(String, String, String, Option<i64>, Option<String>)> = Vec::new();
    {
        let mut s = c.prepare(
            "SELECT n.id,n.kind,n.title,COALESCE(t.priority,50),f.status
               FROM nodes n
               LEFT JOIN tasks t ON t.node_id=n.id
               LEFT JOIN facts f ON f.node_id=n.id
              WHERE t.state='OPEN'
                 OR f.status IN ('UNVERIFIED','CONTESTED','UNCHECKABLE')
              ORDER BY COALESCE(t.priority,50) DESC, COALESCE(t.cost,1) ASC, n.created_at ASC").unwrap();
        let rows = s.query_map([], |r| Ok((
            r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?,
            r.get::<_, Option<i64>>(3)?, r.get::<_, Option<String>>(4)?))).unwrap();
        for r in rows.flatten() { frontier.push(r); }
    }

    let mut out = String::new();
    out.push_str(&format!("REALM  seq={cur}  t={}\n", fmt_hhmm(ts)));
    out.push_str(&format!("frontier={}  live_leases={}  stale={}  tainted={}\n",
                          frontier.len(), live.len(), stale, tainted.len()));
    let leases = if live.is_empty() { "none".to_string() }
                 else { live.iter().map(|(r, o)| format!("{r}->{o}")).collect::<Vec<_>>().join(", ") };
    out.push_str(&format!("leased: {leases}\n"));
    if !frontier.is_empty() {
        out.push_str("\nFRONTIER\n");
        for (id, kind, title, _prio, st) in &frontier {
            let mark = if tainted.contains(id) { " TAINTED" } else { "" };
            let extra = st.as_deref().unwrap_or("");
            let shown: String = title.chars().take(52).collect();
            out.push_str(&format!("  {:<5} {:<8} {:<18}{:<9} {}\n", id, kind, extra, mark, shown));
        }
    }
    if let Some(b) = budget { out = out.chars().take(b * 4).collect(); }
    print!("{out}");
    OK
}

fn fmt_hhmm(ts: i64) -> String {
    // localtime without a crate: use `date` for correctness rather than pretending
    let out = Command::new("date").arg("-r").arg(ts.to_string()).arg("+%H:%M:%S").output();
    match out {
        Ok(o) => String::from_utf8_lossy(&o.stdout).trim().to_string(),
        Err(_) => String::from("--:--:--"),
    }
}

fn cmd_since(cursor: i64) -> i32 {
    let c = open();
    let mut stmt = c.prepare(
        "SELECT cursor,at,actor,op,node_id,detail FROM events WHERE cursor > ?1 ORDER BY cursor").unwrap();
    let rows = stmt.query_map(params![cursor], |r| Ok((
        r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?,
        r.get::<_, String>(3)?, r.get::<_, Option<String>>(4)?,
        r.get::<_, Option<String>>(5)?))).unwrap();
    let mut any = false;
    for (cur, at, act, op, node, detail) in rows.flatten() {
        any = true;
        println!("{cur:6} {} {:12} {:16} {:6} {}",
                 fmt_hhmm(at),
                 act.chars().take(12).collect::<String>(),
                 op,
                 node.unwrap_or_default(),
                 detail.unwrap_or_default().chars().take(48).collect::<String>());
    }
    if !any { println!("(no events since {cursor})"); }
    OK
}

fn cmd_show(id: &str) -> i32 {
    let c = open();
    let row: Option<(String, String, String, i64)> = c.query_row(
        "SELECT kind,title,created_by,created_at FROM nodes WHERE id=?1", params![id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).optional().ok().flatten();
    match row {
        None => die(MISSING, &format!("realm: no such node {id}")),
        Some((kind, title, by, at)) => {
            println!("id      {id}");
            println!("kind    {kind}");
            println!("title   {title}");
            println!("by      {by}");
            println!("at      {}", fmt_hhmm(at));
            let status: Option<String> = c.query_row(
                "SELECT status FROM facts WHERE node_id=?1", params![id],
                |r| r.get::<_, String>(0)).optional().ok().flatten();
            if let Some(st) = status { println!("status  {st}"); }
            println!("\nedges:");
            if let Ok(mut s) = c.prepare("SELECT kind,to_id FROM edges WHERE from_id=?1") {
                if let Ok(rows) = s.query_map(params![id], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))) {
                    for (k, d) in rows.flatten() { println!("  {k:12} -> {d}"); }
                }
            }
            if let Ok(mut s) = c.prepare("SELECT kind,from_id FROM edges WHERE to_id=?1") {
                if let Ok(rows) = s.query_map(params![id], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))) {
                    for (k, d) in rows.flatten() { println!("  {k:12} <- {d}"); }
                }
            }
            OK
        }
    }
}


/// Minimal JSON string escaping so argv round-trips through json.loads().
fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn json_array(items: &[String]) -> String {
    let parts: Vec<String> = items.iter().map(|s| format!("\"{}\"", json_escape(s))).collect();
    format!("[{}]", parts.join(","))
}

fn cmd_run(attach: Option<&str>, argv: &[String]) -> i32 {
    let mut cmd: Vec<String> = argv.to_vec();
    while cmd.first().map(|s| s.as_str()) == Some("--") { cmd.remove(0); }
    if cmd.is_empty() { die(USAGE, "realm: run needs a command after --"); }

    let c = open();
    if let Some(a) = attach {
        if node_exists(&c, a).is_none() { die(MISSING, &format!("realm: no such node {a}")); }
    }
    drop(c);

    let started = now();
    let status = Command::new(&cmd[0]).args(&cmd[1..]).status();
    let code = match status {
        Ok(s) => s.code().unwrap_or(1),
        Err(e) => {
            eprintln!("realm: cannot execute {:?}: {e}", cmd[0]);
            127
        }
    };
    let rev = Command::new("git").args(["rev-parse", "--short", "HEAD"])
        .output().ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();

    let mut c = open();
    txn(&mut c, |t| {
        let id = next_id(t, "attempt");
        t.execute("INSERT INTO nodes(id,kind,title,body,created_by,created_at,updated_at)
                   VALUES (?1,'attempt',?2,'',?3,?4,?5)",
                  params![id, cmd.join(" ").chars().take(120).collect::<String>(), actor(), started, now()])
            .map_err(|e| format!("attempt node insert failed: {e}"))?;
        t.execute("INSERT INTO attempts(node_id,run_id,run_kind,target_id,argv_json,cwd,git_rev,
                    started_at,finished_at,exit_code) VALUES (?1,?1,'run',?2,?3,?4,?5,?6,?7,?8)",
                  params![id, attach, json_array(&cmd),
                          std::env::current_dir().map(|p| p.display().to_string()).unwrap_or_default(),
                          rev, started, now(), code])
            .map_err(|e| format!("attempt row insert failed: {e}"))?;
        if let Some(a) = attach {
            t.execute("INSERT INTO edges(from_id,to_id,kind,at) VALUES (?1,?2,'produces',?3)
                       ON CONFLICT DO NOTHING", params![id, a, now()])
                .map_err(|e| format!("produces edge rejected: {e}"))?;
        }
        emit(t, "run", Some(&id), Some(&format!("exit={code}")))?;
        eprintln!("[realm] {id} recorded exit={code} rev={}", if rev.is_empty() { "-" } else { &rev });
        Ok(())
    });
    code
}

// ---------------------------------------------------------------- sha256

fn sha256_hex(data: &[u8]) -> String {
    // Minimal SHA-256 (FIPS 180-4). Needed for artifact digests to match the Python impl.
    const K: [u32; 64] = [
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
    let mut h: [u32; 8] = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                           0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    let mut msg = data.to_vec();
    let bitlen = (data.len() as u64) * 8;
    msg.push(0x80);
    while msg.len() % 64 != 56 { msg.push(0); }
    msg.extend_from_slice(&bitlen.to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([chunk[4*i],chunk[4*i+1],chunk[4*i+2],chunk[4*i+3]]);
        }
        for i in 16..64 {
            let s0 = w[i-15].rotate_right(7) ^ w[i-15].rotate_right(18) ^ (w[i-15] >> 3);
            let s1 = w[i-2].rotate_right(17) ^ w[i-2].rotate_right(19) ^ (w[i-2] >> 10);
            w[i] = w[i-16].wrapping_add(s0).wrapping_add(w[i-7]).wrapping_add(s1);
        }
        let (mut a,mut b,mut c,mut d,mut e,mut f,mut g,mut hh) =
            (h[0],h[1],h[2],h[3],h[4],h[5],h[6],h[7]);
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = hh.wrapping_add(s1).wrapping_add(ch).wrapping_add(K[i]).wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh=g; g=f; f=e; e=d.wrapping_add(t1); d=c; c=b; b=a; a=t1.wrapping_add(t2);
        }
        h[0]=h[0].wrapping_add(a); h[1]=h[1].wrapping_add(b);
        h[2]=h[2].wrapping_add(c); h[3]=h[3].wrapping_add(d);
        h[4]=h[4].wrapping_add(e); h[5]=h[5].wrapping_add(f);
        h[6]=h[6].wrapping_add(g); h[7]=h[7].wrapping_add(hh);
    }
    h.iter().map(|x| format!("{x:08x}")).collect()
}

// ---------------------------------------------------------------- main

fn usage() -> ! {
    eprintln!("usage: realm <init|add|take|verify|done|cite|link|state|since|show|run|ui> ...");
    std::process::exit(USAGE)
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.is_empty() { usage(); }
    let code = match argv[0].as_str() {
        "init" => {
            let mut path: Option<&str> = None;
            let mut i = 1;
            while i < argv.len() {
                if argv[i] == "--path" && i + 1 < argv.len() { path = Some(&argv[i+1]); i += 2; }
                else { i += 1; }
            }
            cmd_init(path)
        }
        "add" => {
            if argv.len() < 3 { usage(); }
            let mut a = AddArgs { kind: argv[1].clone(), title: argv[2].clone(), id: None,
                body: None, check: None, path: None, priority: 50, cost: 1,
                attempt_limit: 1, depends_on: Vec::new() };
            let mut i = 3;
            while i < argv.len() {
                match argv[i].as_str() {
                    "--id" if i+1 < argv.len() => { a.id = Some(argv[i+1].clone()); i += 2; }
                    "--body" if i+1 < argv.len() => { a.body = Some(argv[i+1].clone()); i += 2; }
                    "--check" if i+1 < argv.len() => { a.check = Some(argv[i+1].clone()); i += 2; }
                    "--path" if i+1 < argv.len() => { a.path = Some(argv[i+1].clone()); i += 2; }
                    "--priority" if i+1 < argv.len() => { a.priority = argv[i+1].parse().unwrap_or(50); i += 2; }
                    "--cost" if i+1 < argv.len() => { a.cost = argv[i+1].parse().unwrap_or(1); i += 2; }
                    "--attempt-limit" if i+1 < argv.len() => { a.attempt_limit = argv[i+1].parse().unwrap_or(1); i += 2; }
                    "--depends-on" if i+1 < argv.len() => { a.depends_on.push(argv[i+1].clone()); i += 2; }
                    _ => i += 1,
                }
            }
            cmd_add(a)
        }
        "take" => {
            if argv.len() < 2 { usage(); }
            let mut ttl: Option<i64> = None;
            let mut i = 2;
            while i < argv.len() {
                if argv[i] == "--ttl" && i + 1 < argv.len() {
                    ttl = Some(argv[i+1].parse().unwrap_or_else(|_| die(USAGE, "realm: --ttl must be an integer")));
                    i += 2;
                } else { i += 1; }
            }
            cmd_take(&argv[1], ttl)
        }
        "verify" => {
            if argv.len() < 2 { usage(); }
            cmd_verify(&argv[1], argv.iter().any(|s| s == "--force"))
        }
        "done" => { if argv.len() < 2 { usage(); } cmd_done(&argv[1]) }
        "cite" => {
            if argv.len() < 2 { usage(); }
            let assume = argv.iter().any(|s| s == "--assume");
            let mut forn: Option<String> = None;
            let mut i = 2;
            while i < argv.len() {
                if argv[i] == "--for" && i + 1 < argv.len() { forn = Some(argv[i+1].clone()); i += 2; }
                else { i += 1; }
            }
            cmd_cite(&argv[1], assume, forn.as_deref())
        }
        "link" => {
            if argv.len() < 4 { usage(); }
            cmd_link(&argv[1], &argv[2], &argv[3])
        }
        "state" => {
            let mut budget = None;
            let mut i = 1;
            while i < argv.len() {
                if argv[i] == "--budget" && i + 1 < argv.len() {
                    budget = argv[i+1].parse().ok(); i += 2;
                } else { i += 1; }
            }
            cmd_state(budget)
        }
        "since" => {
            if argv.len() < 2 { usage(); }
            let c: i64 = argv[1].parse().unwrap_or_else(|_| die(USAGE, "realm: cursor must be an integer"));
            cmd_since(c)
        }
        "show" => { if argv.len() < 2 { usage(); } cmd_show(&argv[1]) }
        "run" => {
            let mut attach: Option<String> = None;
            let mut rest: Vec<String> = Vec::new();
            let mut i = 1;
            while i < argv.len() {
                if argv[i] == "--attach" && i + 1 < argv.len() { attach = Some(argv[i+1].clone()); i += 2; }
                else if argv[i] == "--" { rest = argv[i..].to_vec(); break; }
                else { rest = argv[i..].to_vec(); break; }
            }
            cmd_run(attach.as_deref(), &rest)
        }
        "ui" => {
            // The TUI lives in realm_ui.py until a native port lands. Locate it explicitly:
            // if the script path is missing, python would parse --once as ITS OWN flag and
            // fail with "Unknown option", which is what happened before this fix.
            let mut found: Option<PathBuf> = None;
            if let Ok(p) = std::env::var("REALM_UI") {
                let cand = PathBuf::from(p);
                if cand.is_file() { found = Some(cand); }
            }
            if found.is_none() {
                if let Ok(exe) = std::env::current_exe() {
                    let mut dir = exe.parent().map(|p| p.to_path_buf());
                    while let Some(d) = dir {
                        for cand in [d.join("realm_ui.py"), d.join("../../realm_ui.py")] {
                            if cand.is_file() { found = Some(cand); break; }
                        }
                        if found.is_some() { break; }
                        dir = d.parent().map(|p| p.to_path_buf());
                    }
                }
            }
            let script = found.unwrap_or_else(|| {
                eprintln!("realm ui: realm_ui.py not found (set REALM_UI=/path/to/realm_ui.py)");
                std::process::exit(7)
            });
            match Command::new("python3").arg("-B").arg(&script).args(&argv[1..]).status() {
                Ok(s) => s.code().unwrap_or(1),
                Err(e) => { eprintln!("realm ui: {e}"); 7 }
            }
        }
        "-h" | "--help" | "help" => {
            println!("realm — a shared space for agents\\n");
            println!("  realm init [--path DIR]");
            println!("  realm add <kind> <title> [--check CMD] [--path P] [--priority N] [--depends-on ID]");
            println!("  realm take <task> [--ttl SEC]");
            println!("  realm verify <fact> [--force]");
            println!("  realm done <task>");
            println!("  realm cite <fact> [--assume] [--for NODE]");
            println!("  realm link <src> <kind> <dst>");
            println!("  realm state [--budget N]");
            println!("  realm since <cursor>");
            println!("  realm show <id>");
            println!("  realm run [--attach ID] -- <cmd...>");
            println!("  realm ui [--once] [--ascii] [--cols N] [--rows N]");
            OK
        }
        _ => usage(),
    };
    std::process::exit(code);
}
