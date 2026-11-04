# Dev MCP development intelligence (V2 tools)

`sshpilot-mcp-dev` gives coding agents evidence about this checkout: where a
failure probably lives, what a change touches, which tests have static
evidence for it, and whether a change was actually verified. It does not
write code and has no model inside it. Every claim in a reply points at a
path and line, a test, a Git revision, or is labelled as a heuristic.

The runtime MCP server (`sshpilot-mcp-runtime`) is not affected by any of this.

## The six tools

| Tool | Required | Optional | Returns |
| --- | --- | --- | --- |
| `diagnose_bug` | `symptoms` | `traceback`, `test_name`, `changed_paths`, `base`, `max_items`, `budget` | ranked candidate symbols, the evidence and score for each, subsystem hints, uncertainties, next steps |
| `plan_feature` | `request` | `subsystem`, `symbol`, `budget` | matching symbols and API methods, layers, API involvement, layer obligations tied to real paths, related tests |
| `inspect_impact` | `base` or `paths` | `depth`, `budget` | changed symbols (from diff hunks), dependents, wire-protocol surfaces, dangling references, a test plan |
| `verify_change` | — (`base` defaults to `HEAD`) | `mode` (`static`/`focused`), `suites`, `max_test_files`, `budget` | each check with `executed` and `state`, the test selection and its evidence, `checks_passed`, `coverage_confirmed`, `verification_gaps`, `overall` |
| `get_context` | `reference` | `include_source`, `depth`, `budget` | a source excerpt, callers, callees, API chain and related tests for one symbol |
| `index_status` | — | `rebuild` | refresh statistics, freshness, parse failures |

The same operations are available without the MCP SDK:

```bash
python -m sshpilot.mcp.dev.cli index-status
python -m sshpilot.mcp.dev.cli diagnose-bug --symptoms "..." --traceback-file trace.txt
python -m sshpilot.mcp.dev.cli plan-feature "rename a port forward" --subsystem daemon
python -m sshpilot.mcp.dev.cli inspect-impact --base origin/dev
python -m sshpilot.mcp.dev.cli get-context src/sshpilot/daemon/dispatch.py::RequestDispatcher._handle_open_session
python -m sshpilot.mcp.dev.cli verify-change --base origin/dev --mode focused
```

The CLI prints JSON. It exits 2 for invalid input; `verify-change` exits 0
only when `overall` is `checks-passed`.

### Typical agent workflows

- **Debug a failure.** Call `diagnose_bug` with the traceback, then
  `get_context` on the top candidate. If confidence is `low`, the candidates
  are leads, not a diagnosis; follow `next_steps`.
- **Plan a feature.** Call `plan_feature`, read the patterns with
  `get_context`, and treat `obligations` as a checklist. It never proposes code.
- **Before committing.** Call `inspect_impact(base=...)`, fix what it flags,
  then `verify_change(base=..., mode="focused")`. Add the `suites` it
  recommends when `verification_gaps` asks for them.

## Result conventions

Every V2 reply has `schema` (`sshpilot-dev-intel/1`), `tool`, `freshness`
and `budget`.

**Confidence labels on edges:**

| Label | Meaning |
| --- | --- |
| `verified_static` | A name bound by an import or module-level definition that resolves to an indexed definition, or a literal registration table (`HANDLERS`, `DAEMON_METHOD_CAPABILITIES`, `self._request("…")`). |
| `heuristic` | Plausible but not proven: `self.m()` / `super().m()` dispatch, callbacks passed by reference, GTK `connect("signal", handler)`, re-export hops, test associations. |
| `unresolved` | No indexed definition: attribute calls on objects of unknown type, `getattr` with a computed name, external libraries. These are listed as `possible_callers` / `possible_dependents`, never as dependencies. |

`diagnose_bug` candidates have `high`, `medium` or `low` confidence. `high`
needs an exact traceback frame. Keyword matches alone are always `low`.

**Budget.** Replies default to about 4,800 bytes (around 1,200 estimated
tokens at 4 bytes per token; there is no tokenizer). `budget` takes 800 to
65,536 bytes. Ranked lists are trimmed from their lowest-ranked end, and
every list keeps its top three entries before any list is emptied. The
`budget` block reports `omitted` counts, `used_bytes`, `next_context_ref`,
and `over_budget` if even the minimum does not fit. `verify_change` never
trims its `checks`, because a dropped failure would read as a pass.

**Run states** (`verify_change`): `passed`, `failed`, `skipped`, `not_run` or
`unknown` (timeout or missing tool), each with `executed`. The result keeps
three answers apart:

- `checks_passed`: every executed check passed and at least one selected test
  ran and passed.
- `coverage_confirmed`: always `false`. No coverage is collected, so nothing
  proves the changed lines ran; a test that calls a changed function can still
  miss the changed branch.
- `verification_gaps`: every reason the run falls short. A gap is recorded
  when any selected test was skipped (even one next to passes), when a changed
  symbol has no test that calls or references it (an import alone does not
  count), when a run timed out, when a recommended suite was not run, or when
  the impact analysis was incomplete. Gaps and checks are never trimmed by the
  budget.

`overall` is `checks-passed` only when `checks_passed` holds and there are no
gaps. Otherwise it is `failed` (a check failed), `static-only` (static mode)
or `incomplete`.

## Architecture

```
src/sshpilot/mcp/dev/
  engine/            pure analysis: stdlib ast + sqlite3, no MCP SDK, no subprocess
    extract.py       one file -> definitions + edges with mechanism/confidence
    index.py         versioned SQLite store, incremental refresh, exclusions
    domain.py        client -> wire -> handler -> capability edges (reuses api_surface)
    graph.py         query-time resolution, callers/callees, bounded reverse traversal
    tests.py         static test-to-code association
    context.py       get_context
    impact.py        inspect_impact
    diagnosis.py     diagnose_bug
    planning.py      plan_feature
    verify.py        verify_change (calls the allowlisted execution module)
    models.py        result envelope, confidence labels, budget fitter
    service.py       DevIntelligence: the single entry point for both adapters
  cli.py             JSON CLI adapter
  server.py          MCP adapter (thin wrappers) + the legacy tools
```

**What gets indexed.** Python files under `src/`, `tests/` and `scripts/`,
plus top-level `*.py`. The index holds:

- definitions: modules, classes, functions and methods (with nesting), plus
  module- and class-level assignments, so enum members such as
  `EventType.X` and `Capability.Y` resolve;
- edges: `imports`, `calls`, `references`, `inherits` and `connects_signal`;
- domain edges: `api_to_wire`, `implemented_by`, `wire_to_handler` and
  `uses_capability`.

Every edge records its mechanism (for example `import-alias`, `self-method`
or `getattr-dynamic`). Symbol ids are `<path>::<qualname>`. A duplicate
definition in one file gets `@2`, `@3` and so on, so equal names in
different files or classes never collide.

**Storage.** The index is stored at
`$SSHPILOT_MCP_INDEX_DIR/index-v<schema>.sqlite3`, or by default at
`$XDG_CACHE_HOME/sshpilot-mcp-dev/<checkout>-<hash>/`. That is outside the
checkout, so nothing needs a Git ignore rule. The directory mode is 0700. The
index stores paths, hashes, names, line ranges, signatures (defaults omitted)
and first docstring lines. It never stores file contents, tool inputs,
tracebacks or environment data; a test checks that a password literal in
source never reaches the database file. Different schema versions use
different files. A database that belongs to another checkout is cleared, not
mixed.

**Freshness.**

- Every tool call first stats the indexed tree (mtime and size). It re-hashes
  only changed stats and re-extracts only changed hashes. A warm check takes
  about 64–112 ms on this checkout.
- Cross-file edges are stored by name and resolved at query time, so a change
  in one file never leaves a stale resolved edge in another. No
  dependent-row invalidation is needed.
- `get_context` re-hashes the file just before returning lines. A same-size
  rewrite inside the filesystem's timestamp granularity is caught and
  re-indexed.
- `freshness` reports the Git HEAD, files by parse status, the last refresh,
  and the domain-extractor state.

**Concurrency.** WAL journal, a 120 s busy timeout, and one
`BEGIN IMMEDIATE` per refresh. Two clients serialise and the second finds
nothing to do (tested with two connections in parallel).

**Exclusions.** `.env`, `.env.*`, `*.pem`, `*.key`, `*id_rsa*`,
`*id_ecdsa*`, `*id_ed25519*` and `*/fixtures/keys/*` are never indexed. Add
globs with `SSHPILOT_MCP_INDEX_EXCLUDE=glob1,glob2`. Symlinked directories
are not followed, and a symlinked file is indexed only if its target is
inside the checkout.

**Other knobs.**

- `SSHPILOT_MCP_INDEX_PREFIXES` limits indexing to some prefixes (tests, very
  large checkouts).
- `SSHPILOT_MCP_INDEX_WORKERS=N` turns on parallel extraction for cold
  builds. It is off by default: spawn-based workers re-import the host's
  `__main__`, which is fragile inside some MCP hosts.

**Safety.** The engine imports no `subprocess`, sockets or HTTP; a boundary
test enforces this.

- Git runs only through the read-only `_git.py` allowlist. It now passes
  `--no-ext-diff --no-textconv` (user-configured diff drivers are programs)
  and sets `GIT_OPTIONAL_LOCKS=0` (inspection never writes `.git/index`).
- Tests run only through `execution.py`, from the repository root, with
  `-p no:cacheprovider`. The JUnit report goes to a private temporary
  directory.
- `diagnose_bug` inputs stay in memory. Exception messages and failure text
  come back only as a short redacted preview: key blocks,
  `password=`/`token:`-style values and long opaque strings are replaced.

## What the static analysis cannot see

- No type inference. `obj.method()` on an unknown receiver is `unresolved`.
  Most GTK-to-`DaemonClient` calls look like this (`self._client.open_session`),
  so GTK callers of client methods are reported as possible callers, not as
  dependents.
- GObject introspection, signals emitted by name, plugins registered at run
  time, `getattr` dispatch, decorators that replace functions, and
  monkeypatching are not modelled.
- A call edge is not an execution path. Reverse dependencies say "names
  this", not "runs this".
- Test association is static. Only a test that calls or references the
  symbol counts as direct evidence; importing the symbol or its module is
  weak evidence, labelled so, and still leaves a verification gap. Coverage-based
  association is not implemented.
- `diagnose_bug` matches quoted messages literally in `src/` at query time.
  It cannot see strings that are built dynamically or translated at run time.
- Non-Python files (`.ui`, `.po`, `.json`, `.blp`) are listed as not
  analysed. Changes to the generated API schema trigger the artifact checks.

## Measurements

Measured on this machine (Linux, Python 3.14, 1,298 indexed files,
~470k lines) at commit `62c4534e` with the keyword tier applied in the
working tree. Times vary with load. The cold build was 27.0 s, 41.9 s and
32.3 s in three runs.

### Baseline before the change (Phase 0)

The existing tools re-parse or re-scan the tree on every call:

| Existing tool | Latency | Reply |
| --- | ---: | ---: |
| `find_symbol(_handle_open_session, exact)` | 21.9 s | 154 B |
| `search_source("sessions.open")` | 5.2 s | 12.5 KB |
| `check_frontend_neutrality` | 3.1 s | 119 B |
| `recommend_tests([daemon_client.py])` | 0.64 s | 933 B |
| `trace_api_method(open_session)` | 0.33 s | 342 B |

The existing dev-MCP tests were 99 passing before any change.

### Index

| | |
| --- | --- |
| Cold build (serial) | 27–42 s |
| Warm freshness check | 64–112 ms (median of 5) |
| Index size | 48 MB; 37.9k symbols, 168k edges, 895 domain edges |
| In-process V2 call latency | median 0.49 s (0.09–0.81 s) |
| CLI call (new process, warm index) | 1.6–4.0 s |

### Tool-level benchmark

Run it with `python scripts/benchmark_dev_mcp.py [--json out.json]`. The
tasks are in `tests/mcp/benchmark/dev_intelligence_tasks.json`.

- The answer keys are the source files changed by the real commit that fixed
  each problem.
- The symptoms are written as a reporter would write them, without paths.
- The baseline is what the existing `search_source` tool returns when every
  term of the same text is searched, one call per term, with files ranked by
  distinct terms matched.
- A trivial baseline that always returns the five largest files hits 1/8
  (design) and 1/5 (held-out).

| Set | Tasks | V2 hit@5 | Baseline hit@5 | V2 hit@3 | Baseline hit@3 | V2 mean recall@5 | Baseline mean recall@5 | V2 median reply | Baseline median reply | Calls V2 / baseline |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| design | 8 | 7/8 | 4/8 | 7/8 | 2/8 | 0.59 | 0.35 | 4.1 KB | 143 KB | 9 / 103 |
| held-out | 5 | 5/5 | 1/5 | 4/5 | 1/5 | 0.61 | 0.05 | 4.2 KB | 118 KB | 5 / 59 |

Plus one negative task (a vague report): V2 returned no candidates and
confidence `none`. No misleading-confidence events occurred in 14 tasks.

How to read these numbers honestly:

- **Evidence-only first run.** Before the weak keyword tier existed, V2 hit
  1/8 on the design set; it ignored plain-language symptoms entirely. The
  keyword tier was designed after seeing those eight tasks (it has no
  task-specific words). The held-out five were written afterwards from commit
  titles only and run once. Held-out first-gold ranks were 3, 1, 2, 2 and 5,
  worse than the design set's 1, 1, 2, 1, 2, 1 and 2. Expect some
  over-fitting.
- **Small sample.** These are 13 graded tasks. The answer keys come from
  commits that are already in HEAD, so the fixed code is present.
- **Weak baseline.** The baseline is a mechanical grep. A capable agent greps
  more selectively, so the reply-size gap overstates the saving for a real
  agent.
- **Planning task.** `login-profiles-api` is weak evidence: that API already
  exists, and the obligations are a fixed checklist.
- **Not measured:** end-to-end agent task success, model tokens and agent
  wall-clock time. That needs a pinned agent and model running the same tasks
  with and without these tools. The harness does not do it, so no token or
  time saving is claimed.

## Compatibility and migration

All earlier tools still exist with the same parameters. Changes to them:

- `run_tests`, `run_lint` and `validate_api_artifacts` now run in the
  repository root. Before, a client started elsewhere got "file or directory
  not found" from pytest. Results gain `duration_seconds`.
- `check_frontend_neutrality` adds `status` (`valid`/`invalid`/`unknown`)
  and `unscanned`. An unparseable file now makes `clean: false` instead of
  being skipped.
- `review_public_api` adds `capabilities_referenced`,
  `models_referenced_by_client_signatures`, `complete` and `unscanned`.
  `model_citations` is kept as a deprecated alias of
  `capabilities_referenced`: it always counted capabilities, not models.
- `validate_change` adds `checks` (each with `executed`/`state`),
  `tests_executed: false`, `static_clean` and `verdict`. `clean` keeps its
  old meaning (the static checks that ran passed) and never implies tests ran.

Suggested migration:

| Old call | Prefer |
| --- | --- |
| `recommend_tests` | `inspect_impact` (evidence-based test plan) |
| `validate_change` | `verify_change(mode="focused")` |
| `find_symbol` / `search_source` to locate code | `get_context` / `diagnose_bug` |
| `review_commit` | `inspect_impact(base=...)` |

The generic repository tools (`read_text_file`, `list_directory`,
`search_source`, `find_symbol`, `git_*`, `find_tests`) duplicate what coding
agents already have. They are kept for compatibility and are not
recommended for new workflows. Removing them needs a separate decision once
clients have migrated.

## Not implemented

- **Knowledge records.** The brief's optional records of verified past
  incidents and decisions are not implemented. `plan_feature` and
  `diagnose_bug` cite only the current index.
- **Coverage-based test association.** There is no opt-in coverage
  collection; association is static only.
- **End-to-end agent benchmark.** Not run (see Measurements).
- **Rename tracking.** A renamed symbol appears as a new id. The old id
  disappears; it is not linked.
