# Repository guidance and progress

## Purpose

This repository runs a local LLM agent against projects in a Docker sandbox,
using LM Studio for inference. The intended direction is repeatable, offline
security and correctness investigations for C/C++ with GCC and Clang, Go, Rust,
Python 3.13 and 3.14, and Node.js 24.22.1.

## Working guidance

- Keep this file updated with meaningful progress, verification results, and
  remaining work. Distinguish implemented behavior from proposals.
- Preserve offline execution. Prepare toolchains, project dependencies, and
  vulnerability database snapshots before investigation runs.
- Treat project source, build scripts, test output, and model output as
  untrusted. Keep verification policy outside the model's writable workspace.
- Use disposable project copies and preserve reproducible findings, logs,
  regression tests, and proposed patches as artifacts.
- Never mount host credentials or the Docker socket into a model-controlled
  execution container.
- Prefer bounded investigation cycles with persistent state over unrestricted
  loops. Record why a run stopped and whether verification actually succeeded.
- Do not claim a project is secure because tests pass or the model finishes.
- Keep repository improvements within the user's requested scope. Assessment
  recommendations are not authorization to implement every proposed change.

## Progress log

### 2026-11-01 — Repository assessment completed

Requested work:

- Inspect the project and suggest improvements, especially language support and
  continuous local LLM investigation of security issues and other bugs.
- Add this file and document progress here.

Completed inspection:

- Read all five tracked project files: `README.md`, `Dockerfile`,
  `docker-compose.yml`, `agent.py`, and `.gitignore`.
- Confirmed that the image currently provides Python 3.12, GCC, and basic CLI
  tools; the other requested runtimes and build tools are not configured.
- Identified completion, subprocess lifecycle, output buffering, API recovery,
  isolation, and persistence concerns. Recorded concrete code locations and
  proposed remedies in [IMPROVEMENTS.md](IMPROVEMENTS.md).
- Consulted current official documentation for Node.js 24.22.1, Python
  installation with uv, Docker isolation, LM Studio tool use, LLVM sanitizers
  and fuzzing, Cargo offline dependencies, and Go vulnerability tooling.

Verification completed:

- `docker compose config --quiet` passed with Docker Compose v5.6.1.
- Four isolated standard-library checks confirmed premature completion from
  ordinary prose, action dispatch from reasoning text, failure on non-object
  JSON tool arguments, and child processes surviving command timeouts.
- Reasoning-text command execution was stubbed. The timeout check used only a
  short-lived local child and temporary files. No live model was contacted.

Current implementation status:

- Added this file and [IMPROVEMENTS.md](IMPROVEMENTS.md). Runtime behavior is
  unchanged; the report clearly separates recommendations from implementation.
- No images have been built, target projects run, or network boundaries tested.
- The isolated checks are assessment evidence, not a committed test suite.

Proposed follow-on work, requiring an implementation request:

1. Correct action parsing/completion, process cleanup, bounded output, and run
   outcomes; add fake-LLM regression tests and CI.
2. Add pinned language environments, project manifests, dependency preparation,
   and offline smoke builds/tests.
3. Separate controller, execution workers, and protected verification; use
   disposable project copies.
4. Add a persistent investigation queue, deduplicated findings, reproducible
   evidence, resume support, and bounded continuous campaigns.


### 2026-11-01 — Stage 1: harness reliability implemented

The user authorized proceeding with the proposed improvements in stages.

- Required explicit completion and validated action names, objects, fields,
  types, and sizes. Removed dispatch from reasoning and Markdown fences.
- Replaced whole-output buffering with bounded streaming of merged stdout/stderr.
  Commands use separate process groups cleaned up on exit, timeout, and interrupt.
- Added explicit completed/failed/exhausted/interrupted outcomes and CLI exit
  codes. Continuous runs now stop on failure or exhaustion.
- Limited parser fallback to peg-native errors and added explicit inference
  timeouts/retry limits, SIGTERM cleanup, and Compose init handling.
- Added standard-library fake-LLM and subprocess regression tests and a GitHub
  Actions matrix for Python 3.12/3.13/3.14. Updated README behavior descriptions.
- Validation: 11 regression tests passed locally; Compose configuration passed.
  Hosted CI and live inference have not been run.
- Remaining: deliberately detached processes need container-level cleanup;
  history still uses a message-count limit; verification remains in the same
  trust boundary. Language environments, dependency preparation, isolated
  verification, and persistent investigations remain stages 2–4.

### 2026-11-01 — Stage 1 published; Stage 2 started

- Committed Stage 1 as `4f8ddfa` and pushed directly to `origin/main` as
  requested. User glossary/Claude files were left untracked and untouched.
- Re-ran all 11 tests and Compose validation before committing.
- Stage 2 approach: language worker build targets, operator-owned project
  manifests, networked dependency preparation, and offline smoke checks.

Stage 2 implementation progress:

- Added versioned language targets and an operator CLI for image builds, sanitized
  source snapshots, dependency preparation, immutable image state, and baseline
  checks with networking disabled.
- Added six project fixtures, prepared-image Compose override, usage guide, and
  a CI matrix. Prepared images retain dependencies without a host source mount.
- Local unit suite now has 17 passing tests. Base and prepared Compose configs
  validate. Actual language image builds and offline fixture runs are in progress.

### 2026-11-01 — Stage 2 completed and validated locally

- Built and probed all six language profiles: GCC 12.2 / Clang 14.0.6, Go
  1.27.3, Rust 1.99.1 with Clippy/rustfmt, Python 3.13.18 and 3.14.10, and
  Node.js 24.22.1 (npm 11.19.1).
- Prepared every bundled project and passed each baseline with `--network none`.
  C/C++ checks cover both compilers and ASan/UBSan; Rust includes Clippy; Python
  fixtures install pytest 8.4.2 during preparation and run it offline under both
  interpreters. Go and Node fixtures passed their native test commands.
- Added host-side baseline deadlines with scoped container cleanup on timeout.
  The unit suite now includes a regression check for this cleanup.
- Stage 2 files remain uncommitted. Only Stage 1 was requested for publication.
- Live LM Studio integration and hosted CI remain untested. Fixtures validate
  the preparation mechanism, not every real project's dependencies.
- Prepared agent runs currently discard filesystem edits at exit. Persistent
  evidence/patch export, separate protected verification, and durable campaign
  state remain Stages 3 and 4. See STAGE2.md for usage and limitations.

Final Stage 2 checks: all 18 unit tests, both Compose configurations, and
whitespace validation passed. OpenAI SDK import/client initialization also passed
inside both Python images without network access.

### 2026-11-01 — Stage 2 published; Stage 3 started

- Revalidated all 18 unit tests and both Compose configs, committed Stage 2 as
  `7a6469c`, and pushed directly to origin/main as requested.
- LM Studio responded at http://127.0.0.1:1234/v1/models. The supplied fallback
  is http://10.0.0.2:1234/v1.
- Stage 3 uses a trusted host controller and fresh offline execution containers
  per command, with separate read-only verification and durable run artifacts.

Stage 3 implementation and validation progress:

- Added a trusted host controller with injected execution/verification interfaces
  rather than executing model commands on the host. Inference credentials stay
  with the controller; offline workers receive no host mounts except the trusted
  read-only transfer helper.
- Added per-command containers, a run-specific disposable home volume, read-only
  root filesystems, executable scratch space for compiled tests, resource limits,
  and cleanup that kills detached descendants.
- Added operator-owned protected checks and separate pristine verifier volumes,
  immutable dependencies, read-only candidate source, bounded regular-file
  transfers, durable events/snapshots/results, and Git-compatible patch export.
- Fixed missing parent-directory execute permission in the Stage 2 harness image.
  Preparation repairs existing base images too. Prepared recipe identity now
  includes the Stage 3 schema to avoid retagging prior recipes.
- Docker integration tests passed: protected baselines in all six language
  profiles, rejection of broken source despite rewritten workspace tests,
  detached-process cleanup, symlink rejection, and durable evidence retention.
- Unit suite has 25 passing tests plus 5 opt-in Docker tests (all passed with
  Docker enabled). Git patch tests cover binary files, executable modes, and
  missing final newlines.
- Host disk filled during validation. Approved unused Docker build-cache cleanup
  recovered 2.594 GB; user subsequently cleared host Go/Rust caches, bringing free
  space to about 12 GB. Linux still reports a 50 GiB block device / 49 GiB root
  filesystem. No disk or filesystem resize was performed.
- LM Studio live tests at loopback reached inference and executed commands using
  the 0.5B coder model, but malformed completion actions caused step exhaustion.
  These were correctly recorded as incomplete, with artifacts retained. A bounded
  larger-model check is in progress.

Additional Stage 3 validation:

- Gemma 4 12B (`google/gemma-4-12b`), as recommended by the user, completed a
  live native-tool smoke run at loopback. It created evidence.txt, passed
  protected verification, and completed explicitly; the run artifacts are in
  `artifacts/runs/7f49a3637d6b4a2f90dd4bc44e5453e3/`. A Qwen 14B text-action
  smoke test also completed; tiny-model attempts remain recorded as exhausted.
- Reproduced a Clang 14 ASan startup failure with an empty program: 13/40 PIE
  starts segfaulted. LLVM D147984 documents the matching allocator/ASLR conflict.
  Ranked layout conflict, general runtime failure, and memory-limit hypotheses;
  changing only PIE layout produced 100/100 successful starts.
- Added a regression test and observed it fail before changing the example
  manifest. Applied `-fno-pie -no-pie` only to bundled sanitizer fixture builds.
  No host ASLR setting or production project configuration was changed.

### 2026-11-01 — Stage 3 completed locally

- All 33 tests passed with Docker enabled: 27 unit/subprocess tests and 6 Docker
  integration tests, including all six protected language baselines and the
  100-start sanitizer regression. Both Compose configs and whitespace validation
  passed. Hosted CI has been configured but not run here.
- The C++ original regression was observed failing before the fixture-only fix;
  both the minimized 100-start comparison and full regression pass after it.
- Protected verification now also gates preparation before state publication.
- Gemma 4 12B completed live native-tool validation; documented as the model in
  examples. Explicit model selection and text-only actions remain available.
- Stage 3 implementation is complete. The user subsequently authorized direct
  publication to main. Existing user glossary and Claude files were not changed.
  Stage 4 persistent campaigns remain future work.
- See STAGE3.md for setup, run commands, retained artifacts, candidate replay,
  transfer limits, and the remaining container/disk-isolation limitations.

### 2026-11-01 — Domain glossary established

- Applied the requested domain-modeling skill to the existing empty
  [GLOSSARY.md](GLOSSARY.md) and linked it from README.md.
- Defined preparation, investigation, acceptance, and retained-result terms
  against the current controller, execution boundary, preparation CLI, and tests.
  Distinguished task/run/step, prepared state/run outcome, workspace/protected
  checks, baseline/candidate, and completion request/accepted completion.
- Cross-checked edge cases: rejected completion can end in step exhaustion;
  candidates and proposed patches can exist without accepted completion; the
  original snapshot includes preparation changes rather than necessarily
  matching the operator's source checkout. Passing verification is not a
  security verdict.
- Kept implementation details and future campaign design out of the glossary.
  No architectural decision or runtime behavior changed. Persistent campaign
  terminology remains for Stage 4 design when its meanings are resolved.
- Verification: definitions reviewed against code and existing regression cases;
  documentation whitespace and local glossary links checked. Runtime tests were
  not rerun for this documentation-only change.

### 2026-11-01 — GitHub Actions references pinned

- Pinned checkout v4.2.2 and setup-python v5.6.0 to full commit SHAs in both
  workflows, addressing the four reported mutable action references.
- Verified the references and whitespace locally; hosted workflows have not run
  with these pins yet.

### 2026-11-01 — Stage 4 campaign foundation

- Confirmed Stage 3 commit `3cc7710` and subsequent glossary and workflow fixes.
- Added bounded sequential campaigns with durable task and attempt state, exact task deduplication, resume, locking, stop reasons, and retained run artifacts. Each attempt starts from the prepared project.
- Finding extraction/deduplication, candidate carryover, and automatic scheduling remain future work; see STAGE4.md. Campaign state and outputs require operator review.

### 2026-11-01 — Campaign finding records

- Added operator-authored finding observations with explicit status, links to retained run artifacts, and exact location/root-cause grouping that preserves all observations. This is not automatic validation or reproducibility verification.
- Added a regression test for grouping and evidence path validation. Automatic extraction, fresh-worker reproducer replay, coverage and stall tracking, and target selection remain outstanding.

### 2026-11-01 — Reproducer replay and persistent attempt limits

- Added operator-selected command replay against retained original and candidate snapshots in separate fresh offline workers. Reports preserve command text, snapshot identities, exit codes, and bounded diagnostics for review and finding evidence links.
- Added persistent per-task attempt limits (default three) to bound repeated unsuccessful investigations across resumes.
- Validation: 37 tests discovered, 31 passed and 6 opt-in Docker tests skipped. New tests cover separate replay workers, retained results, recorded-run selection, and retry limits. Actual Docker replay and live inference were not run in this update.
- Replay status records an exit-code comparison, not independent confirmation of a bug. Automated target selection, finding extraction, coverage tracking, repeated-command detection, and reproducer minimization remain outstanding.

### 2026-11-01 — Stage 4 completed and validated locally

- Added structured operator targets with scopes and priorities, persistent fair
  selection, invocation time budgets, explicit continued selection after failures,
  recorded attempted scopes, and terminal reasons when no targets remain eligible.
- Added repeated command/result termination in the controller and unchanged-source
  detection across unsuccessful attempts. Stalled targets are retained and skipped.
- Campaign prompts request hypotheses, necessary conditions, and structured reports.
  Bounded candidate-report imports preserve untrusted proposals and exact
  location/root-cause grouping. Only explicit operator review writes decisions to
  the reviewed findings ledger. Model statuses, host paths, and reproducer text
  cannot authorize execution or confirmation.
- New campaigns check prepared-state identity. State replacement also syncs the
  containing directory. Existing version 1 campaigns remain compatible without the
  new integrity check.
- Validation: all 45 tests passed with Docker enabled, including all six protected
  language baselines, the sanitizer regression, proposal import/review, known
  original failure and candidate success on the synthetic fixture, replay timeout
  handling, repeated-command termination, and retained artifacts. Hosted CI and
  live model campaign quality were not tested in this update.
- Retained an additional synthetic workflow/replay at
  `artifacts/campaigns/stage4-validation-7a8b02b6/`. Its model is a fake client; its
  proposed finding was explicitly rejected. The expected replay failure is the
  fixture's missing evidence file, not a discovered application vulnerability.
- Stage 4's bounded campaign workflow is complete locally. Automatic reproducer
  minimization, scanner-generated targets, semantic deduplication, and candidate
  carryover remain optional extensions. Storage quotas and other limitations from
  Stage 3 remain. See STAGE4.md for commands, budgets, trust boundaries, and limits.

### 2026-11-02 — SonarQube Cloud triage and first fixes

- Triaged all 44 open SonarQube Cloud issues on `a82b01a` (no hotspots; gate
  failing on new-code security rating C). 3 real, 20 judged false positives
  (operator-only CLI path rules S8707/S2083, intentional absolute verifier
  import, dependency-free Rust fixture), 2 design decisions (LAN HTTP fallback),
  19 optional maintainability items. Sonar statuses not changed yet.
- Fixed text-action parsing: the `<bash>`/`<done>` regexes backtracked
  super-linearly on unterminated whitespace (over 60 s for ~8 KB in-process,
  outside any inference timeout). Replaced with linear prefix/suffix checks;
  added regression tests that hung before the fix.
- Moved test bundle construction outside `assertRaises`; added
  `--only-binary :all:` to the image's pip install (confirmed wheels exist for
  Python 3.13 and 3.14).
- Validation: 39 tests discovered, 33 passed, 6 Docker tests skipped. Images
  were not rebuilt.
- Follow-up: addressed the remaining 18 confirmed maintainability issues.
  Split agent dispatch/main, controller run, transfer export, sandbox manifest
  validation/CLI dispatch, and campaign resume/finding validation into helpers
  (behavior preserved; export still checks size before reading). Added
  constants/helpers for duplicated literals and a shared campaign lock helper.
  Sorted Dockerfile packages and merged the base-stage RUN steps.
- Validation: 39 unit tests (33 passed, 6 skipped); complexipy reports every
  function in the edited files at cognitive complexity 15 or below. Rebuilt and
  prepared the python313 profile; 5 Docker integration tests passed with
  SANDBOX_DOCKER_TESTS=1 (C++ sanitizer test skipped). Other profiles were not
  rebuilt. Sonar results will be confirmed on the next analysis.
- Set runtime minimums: Python 3.13 (`pyproject.toml` `requires-python`, a
  `sandbox.py` startup check, CI matrix now 3.13/3.14) and Node.js 24.22.1
  (`engines` in the Node fixture). README and STAGE3 document both. Images were
  already pinned to Python 3.13.18/3.14.10 and Node 24.22.1. Validation: 41
  tests (35 passed, 6 skipped); the 3.12 rejection is covered by a mocked test,
  as no 3.12 interpreter is installed. Node fixture was not re-prepared.

### 2026-11-02 — Stage 4 branch updated onto refactored main

- Merged main (PR #3) into the Stage 4 branch. Resolved campaign.py by keeping
  Stage 4 behavior (targets, budgets, stall detection, report import, status and
  review) on the refactored structure: shared lock helper, file-name constants,
  and helpers for target parsing, eligibility, attempt outcome, stall marking
  and stop reasons. Moved status/review CLI dispatch into run_campaign_command.
  Extracted per-finding proposal validation in investigation.py and moved test
  bundle construction outside assertRaises.
- Sonar issues on PR #2: 6 path rules (S8707/S2083 in save/status/review) judged
  false positives, as campaign files are controller-written host state; S3776,
  S1192 and S5778 addressed by this merge.
- Validation: 49 tests (41 passed, 8 skipped); complexipy reports all functions
  at 15 or below; 7 python313 Docker integration tests passed via discover
  (C++ test skipped). Other profiles were not rebuilt.
- Review follow-up (Greptile, Sonar): repetition detection now compares a
  SHA-256 of the complete command output stream (computed in bounded_process)
  instead of the 8,000-byte retained text, so runs differing only after the
  limit no longer stall a target. Campaign status observations include the
  finding index that review() expects. Moved controller run callbacks into
  RunSession to clear the remaining S3776 issue. Validation: 51 tests (43
  passed, 8 skipped); 7 python313 Docker integration tests passed.

### 2026-11-02 — SonarQube CI analysis with coverage

- Automatic analysis was disabled in SonarQube Cloud by the user; SONAR_TOKEN
  is configured for Actions and Dependabot.
- Added `.github/workflows/sonarqube.yml`: builds and prepares python313, runs
  the full suite including Docker boundary tests under coverage.py 7.16.2, and
  scans with sonarqube-scan-action v8.3.0 (pinned SHA; v8.4.0 was under two
  weeks old). The token is passed only to the scan step; checkout does not
  persist credentials; fork PRs without the secret skip the scan.
- Added `sonar-project.properties` (tests indexed separately, examples excluded
  from coverage, Python 3.13/3.14) and coverage settings in `pyproject.toml`
  with relative paths.
- Validation: local coverage run, 51 tests passed (1 skipped), 76% combined
  line/branch coverage; actionlint passed for all workflows. The hosted
  workflow and scan run on the PR.
- Added `.github/workflows/osv-scanner.yml` from the user's template: OSV-Scanner
  reusable workflows v2.7.0 (SHA verified against the annotated tag, released
  2026-10-12) for PRs/merge groups and push/schedule/manual runs, scanning
  `requirements-controller.txt` as a requirements lockfile plus a recursive
  walk. Local osv-scanner run found go.mod and the controller pins with no
  known vulnerabilities; actionlint passed.
- Applied Greptile's README fix: the local coverage recipe now installs the
  pinned coverage version and writes coverage.xml.
- First hosted OSV run scanned both manifests but failed uploading SARIF:
  code scanning is not enabled on this private repository (requires GitHub
  Code Security). SARIF upload is now gated on the `OSV_UPLOAD_SARIF`
  repository variable (off by default); scans still fail on vulnerabilities.

### 2026-11-02 — Go and Rust fixture minimums; C++ smoke assertion

- Declared fixture toolchain minimums: Go 1.26 (`go` directive) and Rust 1.88
  (`rust-version`); images remain Go 1.27.3 and Rust 1.99.1. README documents
  all fixture minimums; the minimum-runtime test covers them.
- Addressed Sonar cpp:S6994 by making the C++ smoke program assert on a
  runtime call to the fixture's `add()` instead of a constant expression, so
  the GCC and sanitizer builds exercise project code.
- Validation: unit suite passed; go, rust and cpp images built, prepared and
  checked offline; protected baselines for all three and the 100-start
  sanitizer regression passed.

### 2026-11-02 — sandbox.py command-line tests

- Main's quality gate failed on new-code coverage (78.3% < 80%); nearly all
  uncovered new lines were sandbox.py argument parsing and dispatch.
- Added tests/test_sandbox_cli.py: 12 Docker-free tests driving `main()` for
  build/prepare/check/verify/run and every campaign subcommand with mocked
  targets, checking forwarded arguments, defaults, parser rejections, exit codes
  and error mapping. Deliberately dropping `stall_limit` or inverting the resume
  exit code made them fail.
- Validation: 63 tests passed (1 skipped) with python313 Docker tests; local
  coverage sandbox.py 46% -> 84%, total 76% -> 83%.

### 2026-11-02 — IMPROVEMENTS.md status refresh

- Reviewed IMPROVEMENTS.md against the current tree. Kept the original assessment
  text, nested it under "Original assessment", and added a dated status summary
  (implemented / partially implemented / open) plus a status note per section.
- Pinned historical code references to `69e90db` so they no longer appear to point
  at current code.
- Newly recorded open items: `.env.example`, fixed Compose container names,
  unpinned `alpine/socat:latest`, legacy `agent.py` model fallback, and the README
  context-overflow claim for `MAX_HISTORY_MESSAGES`. Documentation-only; runtime
  tests were not rerun.

### 2026-11-02 — Compose hardening and required model/host

- `docker-compose.yml`: proxy pinned to `alpine/socat:1.8.1.1` with digest (1.8.1.3
  was two days old), fixed container names removed, `read_only` root filesystems
  with `/tmp` tmpfs, dropped capabilities and `init` on the proxy. `LM_STUDIO_HOST`
  is now required (no `10.0.0.2` default); added `.env.example`.
- `agent.py` no longer auto-selects the first LM Studio model; `LLM_MODEL` is
  required. README updated; unit test added.
- Validation: 64 tests (56 passed, 8 skipped); both Compose configs validate;
  the read-only sandbox can write `/tmp` and the workspace but not `/home/agent`
  or `/opt`; the proxy starts read-only. No live inference or full agent run.
  Behavior change: existing setups need a `.env` with `LM_STUDIO_HOST` and `LLM_MODEL`.

- PR #7 review follow-up: removed the unused `client` parameter from
  `get_active_model` (Sonar S1172); prepared Compose override sets
  `read_only: false` because the project is baked into the image workspace and
  edits are discarded at exit; README Quick Start now creates `.env` before
  `docker compose build`. 64 tests (8 skipped) and the merged Compose config
  were checked.

### 2026-11-02 — Estimated token budget for history

- `trim_history` now also bounds retained history by an estimated token budget
  (`MAX_CONTEXT_TOKENS`, default 12000, about 3 characters per token; 0 disables),
  keeping the system prompt, task and newest complete tool exchange and never starting on an orphaned tool
  result. The estimate is approximate (no tokenizer offline), ignores the tool
  schema, and a single oversized newest message can still exceed the budget.
  README corrected; Compose and `.env.example` pass the new setting.
- Validation: 3 new tests in `tests/test_agent.py` pass with the existing ones
  (17 in that module). No live inference was run. Compaction summaries remain open.
- Added a License section to README.md pointing at the MIT `LICENSE` file.
- PR #8 review follow-up: an oversized newest tool result was dropped together with
  its assistant call's orphan check, hiding the latest output. Trimming now extends
  backwards to keep the whole newest exchange; regression test added (68 tests, 8 skipped).

### 2026-11-02 — Offline dependency advisory snapshots

- Added `advisories.py` and `sandbox.py advisories fetch|scan`. `fetch` stores
  hash-verified OSV databases (Go, PyPI, crates.io, npm) with fetch timestamps,
  writing metadata only after complete verified downloads. `scan` extracts
  lockfiles from a prepared image with networking disabled and checks them with
  OSV-Scanner `--offline`; missing databases, no lockfiles, a tampered or stale
  snapshot, or scanner failure yield exit 2 and are never reported as clean.
- Validation: 84 tests (76 passed, 8 skipped), including a real OSV-Scanner run
  against a synthetic snapshot. Manually fetched the live Go and crates.io
  databases and scanned the prepared Go and Rust fixtures (no advisories, exit 0)
  and the Python fixture (no lockfile, exit 2). Images were not rebuilt.
- Not done: govulncheck, cargo-audit and pip-audit integration, reachability
  analysis, npm scanning against a live npm snapshot (220 MB, not fetched here),
  and feeding reports to the model. Snapshots stay on the host.
- Same PR: added pinned analysis tools to the images (Staticcheck v0.8.2, Ruff
  0.17.1, Hypothesis 6.170.1, ESLint 10.13.0; all at least two weeks old) with
  offline build probes. Rebuilt go, python313, python314 and node; re-prepared the
  go, node and python313 fixtures (offline checks and protected baselines pass).
  libFuzzer (cpp) and `go test -fuzz` found planted crashes with networking
  disabled. govulncheck, cargo-audit and cargo-fuzz are deferred. cpp and rust
  images were not rebuilt. 87 tests (79 passed, 8 skipped).

### 2026-11-02 — Condensed record of trimmed history

- `agent.py` now builds a deterministic, bounded record (command, exit status, first
  and last output lines) of messages that leave the history window and appends it to
  the task message in each prompt view; the stored transcript and original task are
  unchanged. Window budget reserves space for it. `MAX_SUMMARY_CHARS` (default 3000,
  `0` disables) is wired through Compose, `.env.example` and README. The cap covers the
  whole record including header and omission note, and unfinished text-action pairs
  carry across trims (PR #10 review).
- Chosen over a model-written summary because it needs no extra inference, cannot be
  steered by the model into dropping evidence, and is testable offline. It loses the
  model's reasoning and hypotheses; excerpts are labelled untrusted program output.
- Validation: 8 new tests in `tests/test_agent.py`; full suite 95 tests passed with
  `SANDBOX_DOCKER_TESTS=1`; Compose config validates. The record is not yet
  persisted in run artifacts.
- Live check (Gemma 4 12B, python313 fixture, `MAX_HISTORY_MESSAGES=6`, 14 steps):
  captured prompts confirm the record reaches the model (7 entries by step 11, task
  intact). One run with the record and one control with `MAX_SUMMARY_CHARS=0` both
  ended with "Repeated identical command and result reached the stall limit" and
  neither wrote evidence.txt. With the record the model still re-ran `cat
  calculator.py` and `ls -F` that were listed in it. Two runs on a two-file fixture
  show no measurable benefit; effect on longer investigations of real projects is
  unmeasured. Runs: `artifacts/runs/80e5b28287ae472ab0b3a9c52a2f3616` (record) and
  `77fc9e22a8774675a22a48567735ef5d` (control).

### 2026-11-05 — Debian 13 (trixie) base for Python and C/C++ images

- Snyk's report for `python:3.13.18-slim-bookworm` listed 117 issues (1 critical,
  5 high); its suggested Alpine/3.16-rc bump (PR #11, closed) would have broken the
  apt/glibc-based Dockerfile and the Python 3.13/3.14 minimums. Snyk scored
  `python:3.16-rc-slim-trixie` at 82 issues (0 critical, 3 high, 1 medium); the exact
  3.13/3.14 trixie tags used here were not scanned by Snyk.
- Moved the shared base to `python:3.13.18-slim-trixie` and `python:3.14.10-slim-trixie`
  (version tags, matching the other images; both were rebuilt on 2026-11-05, so they do not meet the
  two-week age rule). Clang 14 is not in trixie: the cpp profile now uses Clang 19
  (clang-tidy, compiler-rt, llvm 19); GCC 12 is unchanged. Go, Rust and Node
  toolchains are still copied from bookworm images (glibc 2.36 binaries on 2.41).
- Validation: rebuilt and probed all six profiles, prepared every fixture with
  offline baselines, and ran 95 tests with `SANDBOX_DOCKER_TESTS=1` (all passed,
  including the 100-start sanitizer regression on Clang 19). Remaining Snyk findings
  were not rescanned; the fixture-only `-fno-pie` flags were kept.

### 2026-11-05 — Snyk rescan of the trixie base

- Snyk scanned `python:3.13.18-slim-trixie`: 81 issues (0 critical, 3 high, 1 medium,
  77 low), down from 117 (1 critical, 5 high, 11 medium, 100 low) on the bookworm base.
- The remaining high/medium findings are Debian 13 packages with no fix available:
  zlib1g out-of-bounds write (CVE-2026-76844, CVE-2026-85091; high), libacl1 link
  following (CVE-2026-54369; high) and TOCTOU (CVE-2026-54370; medium), and libattr1
  link following (CVE-2026-54371; high). `apt-get update` in the image showed no newer
  package versions on 2026-11-05; the advisories themselves were not read.
- Assessment (not measured): zlib is the one to watch because the images handle
  untrusted inputs, but exploitation would stay inside offline, read-only,
  capability-dropped containers. The acl/attr findings need an attacker racing
  filesystem operations inside the container. Snyk lists no known exploit for any.
- Decision: accept and re-check. Snyk is configured as a passive reporter (no fix PRs,
  findings not ignored), so they will show as fixed once Debian ships patches. A future
  failing severity threshold on the Snyk check would block on these until then.
- Re-check: rebuild images and rescan when Debian updates zlib, acl or attr; do not
  switch to Alpine for these (it breaks the apt/glibc Dockerfile).
