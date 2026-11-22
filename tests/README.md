# Realm tests

Run the unified stdlib suite from the repository root:

```sh
python3 -I -B -S -m unittest discover -s tests -v
```

All tests use the real CLI in disposable projects and SQLite stores. No live
`.realm` database, models, external providers, services or devices are touched.
Entrypoint, schema, Python reference, UI and optional Rust executable bytes are
captured once per Codex test process, then copied into every fixture. Receipts
under `coordination/test-runs/` identify the tested source;
the implementation owner must keep that source/schema pair coherent.

To exercise the same Codex cases against both backends and retain an honest
receipt, choose a new output directory:

```sh
python3 -I -B -S coordination/check_cli.py --suite codex --impl py --impl rust --out coordination/test-runs/your-new-run
```

The runner requires an existing Rust build, does not build or install anything,
and fails if tests fail, time out, discover zero cases, or hashed inputs change
during the run. It saves commands, implementation selection, source/binary
identities and raw stdout/stderr; it never overwrites a previous receipt.
Use `--suite canonical`, `--suite ui`, or `--suite rust` for the other entrypoints.
The receipt identifies a tested binary; it does not certify a reproducible build.

Gate refusals require exit 2, lease ownership conflicts 4, failed checks 5,
and uncheckable verification 8. A passing command with a warning cannot satisfy
a gate-refusal assertion. The author's explicitly permitted trivial-check citation
must retain its warning; other agents must be refused.

The suite tests the owner's current contract. It does not claim filesystem or
hostile-agent sandboxing, exhaustive prompt/model quality, automatic freshness
from file changes, or same-name stale-worker fencing (there is no done-token flag).
