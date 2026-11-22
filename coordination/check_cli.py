#!/usr/bin/env python3
"""Run bounded Realm checks and keep outputs with before/after source identities."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SUITES = {"codex": "tests/codex", "canonical": "tests",
          "ui": "tests/ui", "rust": "tests/rust"}


def hashes(suite):
    names = {"realm", "realm-py", "realm_ui.py", "realm_dispatch.py", "realm_workers.py", "schema.sql",
             "rust/Cargo.toml", "rust/Cargo.lock", "rust/target/release/realm",
             "coordination/check_cli.py"}
    names.update(str(p.relative_to(ROOT)) for p in (ROOT / SUITES[suite]).rglob("*.py"))
    names.update(str(p.relative_to(ROOT)) for p in (ROOT / "rust/src").rglob("*.rs"))
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in sorted(names) if (ROOT / name).is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path,
                        help="new receipt directory (never overwrite a previous run)")
    parser.add_argument("--impl", action="append", choices=("py", "rust"), required=True)
    parser.add_argument("--suite", choices=SUITES, default="codex")
    args = parser.parse_args()
    if "rust" in args.impl and not (ROOT / "rust/target/release/realm").is_file():
        parser.error("Rust binary is absent; ask the implementation owner to build it")
    dest = args.out if args.out.is_absolute() else ROOT / args.out
    dest.mkdir(parents=True, exist_ok=False)
    before = hashes(args.suite)
    runs = []
    for impl in dict.fromkeys(args.impl):
        command = [sys.executable, "-I", "-B", "-S", "-m", "unittest", "discover",
                   "-s", SUITES[args.suite], "-v"]
        start = time.monotonic()
        child = subprocess.Popen(command, cwd=ROOT, env=dict(os.environ, REALM_IMPL=impl),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = child.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = child.communicate()
        (dest / f"{impl}.stdout.txt").write_text(stdout)
        (dest / f"{impl}.stderr.txt").write_text(stderr)
        count = re.search(r"^Ran (\d+) tests? in ", stderr, re.MULTILINE)
        run = {"implementation": impl, "command": command, "exit_code": child.returncode,
               "timed_out": timed_out, "tests": int(count[1]) if count else 0,
               "elapsed_seconds": round(time.monotonic() - start, 3)}
        runs.append(run)
        print(json.dumps(run), flush=True)
    after = hashes(args.suite)
    good = before == after and all(r["exit_code"] == 0 and r["tests"] > 0
                                   and not r["timed_out"] for r in runs)
    receipt = {"observed_utc": datetime.now(timezone.utc).isoformat(),
               "suite": args.suite, "before": before, "after": after,
               "source_unchanged": before == after, "runs": runs,
               "qualified": good,
               "scope": "Disposable-store tests; identifies tested files, not a reproducible native build."}
    (dest / "RECEIPT.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"qualified": good, "source_unchanged": before == after,
                      "receipt": str(dest / "RECEIPT.json")}), flush=True)
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
