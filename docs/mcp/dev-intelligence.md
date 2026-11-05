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
  when any test in the focused run or in a requested suite was skipped (even
  one next to passes; suites are run with per-test outcomes too), when a changed
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

- Every tool call first stats the indexed tree (size, mtime and ctime;
  ctime catches a `chmod` that leaves size and mtime unchanged). It re-hashes
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

Measured on this machine (Linux, Python 3.14; about 1,300 indexed files and
470k lines at HEAD). Times vary with load.

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
| Cold build, serial (MCP server default) | 27–42 s (three runs, varies with load) |
| Cold build, `SSHPILOT_MCP_INDEX_WORKERS=8` (benchmark script) | 6.4–7.8 s (six snapshots, 1,194–1,299 files) |
| Warm freshness check | 43–112 ms |
| Index size | 45–48 MB; 37.9k symbols, 168k edges, 895 domain edges (HEAD) |
| In-process V2 call latency | median 0.49 s (0.09–0.81 s) |
| CLI call (new process, warm index) | 1.6–4.0 s |

### Tool-level benchmark

Run it with `python scripts/benchmark_dev_mcp.py [--checkout prefix|head]
[--json out.json]`. The dataset, `tests/mcp/benchmark/dev_intelligence_tasks.json`,
is frozen at version 2 (14 tasks). Its answer keys are the source files
changed by the real commit that fixed each problem, and the symptoms are
written as a reporter would write them, without paths.

**Every task runs on the checkout immediately before its fix.** The harness
extracts `git archive <fix>^` into a temporary directory and indexes that
snapshot from scratch, so names and code introduced by the fix cannot leak
into the answer. `--checkout head` reproduces the earlier run on the current
tree for comparison.

Each task is answered three ways, all measured on the same pre-fix snapshot:

- `v2`: one `diagnose_bug` (or `plan_feature`) call at the default budget.
- `search_all`: the original dev MCP `search_source` tool, run once per term
  of the symptom text. This is a mechanical upper bound on search output, not
  how a good agent searches.
- `selective`: an emulation of an efficient searcher. It probes every term
  for matching file names only (like `rg -l -i`), keeps the three rarest terms
  that hit 1–30 files, reads their match lines, and ranks files by distinct
  kept terms.

Pre-fix results, measured 2026-11-05 at `e076181b`:

| Set | Tasks | `v2` hit@5 | `search_all` hit@5 | `selective` hit@5 | `v2` hit@3 | `selective` hit@3 | `v2` recall@5 | `selective` recall@5 | Median reply `v2` / `search_all` / `selective` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| design | 8 | 7/8 | 3/8 | 2/8 | 7/8 | 1/8 | 0.59 | 0.17 | 4.1 / 139 / 58 KB |
| held-out | 5 | 4/5 | 1/5 | 3/5 | 3/5 | 2/5 | 0.36 | 0.38 | 4.2 / 117 / 53 KB |
| all | 13 | 11/13 | 4/13 | 5/13 | 10/13 | 3/13 | 0.50 | 0.25 | 4.1 / 125 / 55 KB |

Calls across all 14 tasks: `v2` 14, `search_all` 162, `selective` 192 (one
probe per term; a real agent may batch these). On the vague negative task V2
returned no candidates and confidence `none`. There were no
misleading-confidence events.

How to read these numbers:

- **Leakage was small but real.** On the current tree V2 hit 12/13; on
  pre-fix snapshots it hits 11/13. `closed-transport-reported-on-quit` was
  only found with the fix present.
- **On unseen tasks the advantage is modest.** On the held-out five, V2
  (4/5 hit@5, recall 0.36) is close to the selective searcher (3/5, recall
  0.38), at about a twelfth of the reply bytes in one call. The larger gap on
  the design set is partly over-fitting: the keyword tier of `diagnose_bug`
  was designed after seeing those eight tasks, and the first, evidence-only
  version hit 1/8 there.
- **The planning task is not evidence.** `login-profiles-api` counts as a
  hit because `plan_feature`'s obligations are a fixed checklist of API paths;
  it says nothing about retrieval quality.
- **Small sample.** These are 13 graded tasks, and the baselines are
  emulations, not an agent.
- **Agent level:** see the end-to-end pilot below. It showed no difference
  between the arms, and agents rarely used the tool.

### End-to-end agent experiment (pilot, 2026-11-05)

This pilot asks one question: does the engine change whether an agent fixes a real bug, and at what cost? The protocol and harness are in `scripts/agent_experiment/`.

**Setup:**

- **Tasks.** Six historical bugs whose fixing commits added tests that fail on the pre-fix tree by an assertion and pass on the fix. Each agent worked in a `git archive` of the pre-fix commit with no history, and got only the frozen v2 symptom text.
- **Arms.** "plain" (shell, search, file tools) and "devintel" (the same tools plus the engine's CLI).
- **Model and conditions.** The same model in both arms, one run per task and arm, all 12 runs started together.
- **Grading.** The fixing commit's own tests, applied in a clean copy. The grader was checked in both directions: the real fixes resolve 6/6 and the untouched trees 0/6.
- **Audit.** A transcript audit found no forbidden access (main checkout, `git`, MCP, web).

| Arm | Resolved (fix commit's tests) | Resolved (adapted tests) | Tokens reported | Tool calls | Wall time | Output tokens | `devintel` calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| plain | 3/6 | 5/6 | 480k | 124 | 1,081 s | 68.6k | 0 |
| devintel | 3/6 | 5/6 | 460k | 121 | 1,067 s | 68.2k | 4 |

Per task, the two arms reached the same outcome on all six bugs:

| Task | Resolved by both | Note |
| --- | --- | --- |
| saved-passphrase retry | yes | |
| SFTP short reads | yes | |
| sidebar recovery | yes | |
| quit transport error | adapted only | |
| daemon reply priority | adapted only | |
| Windows stale endpoint | neither | plain passed 7/8 adapted assertions, devintel 6/8 |

Paired differences per task were small in both directions: tokens −30% to +11%, tool calls −11 to +7.

**Reading the result:**

- **No effect on outcomes or cost was shown.** Both arms solved the same bugs. Total tokens (−4%), tool calls (−2%) and wall time (−1%) differ by less than the run-to-run variation between tasks. With one run per arm on six tasks, this cannot separate a small effect from noise.
- **Agents barely used the tool.** In four of six runs the agent called `diagnose_bug` exactly once, usually as its first action. In the other two it never called the tool. No agent called `get-context`, `inspect-impact` or `plan-feature`. The pilot therefore measures "one `diagnose_bug` call", not the tool suite.
- **The hard parts were not about finding code.** Plain search found the right file in every run. Where agents fell short, it was design (the Windows endpoint fix missed the slow-refusal and PID-reuse cases), not locating the bug.
- **Strict grading under-counted.** Three of the six fix commits' tests encode implementation details: a private constant, exact log wording, or a test double without new helper methods. Two of those caused correct fixes to be graded as failures. The "adapted" column keeps every behavioural assertion and drops only that coupling. Every adapted test fails on the pre-fix tree and passes on the real fix.

**Limits.** The `devintel` arm uses the CLI, not MCP; access rules were instructed and audited, not sandboxed; the sample is one run per arm on six tasks; and these are tasks where plain search already localises the bug.

**What this means.** Making the engine smarter is not the bottleneck right now; agents getting value from it is. Before adding capability such as receiver inference, test whether agents use `get_context` and `inspect_impact` when prompted to (for example by their instructions or by tool descriptions), and repeat the experiment with several runs per arm and harder localisation tasks.

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
- **Parallel builds in the MCP server.** The server builds serially by
  default; the 6–8 s parallel build is measured only in the benchmark script.
- **Rename tracking.** A renamed symbol appears as a new id. The old id
  disappears; it is not linked.
