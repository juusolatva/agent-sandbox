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
