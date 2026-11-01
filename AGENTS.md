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
