# Assessment and proposed improvements

Assessed on 2026-11-01 against the initial commit `69e90db`.
These are recommendations, not implemented runtime features.

The existing non-root container, capability restrictions, resource limits, and
separate inference proxy are useful foundations. The largest improvements for
multi-language bug hunting are reliable execution, reproducible offline project
environments, independent verification, and persistent investigation state.

## 1. Fix the agent's completion and execution behavior first

| Priority | Evidence in current code | Proposed change |
| --- | --- | --- |
| First | `agent.py:320` accepts any nonempty text as completion, even a plan to inspect files. | Require a validated `finish_task` call or explicit completion action. Ordinary prose must not end a run. |
| First | `agent.py:242` substitutes `reasoning_content` for empty content, then interprets it as executable actions. | Keep reasoning separate from action dispatch. Execute only validated, explicitly emitted actions. |
| First | `agent.py:100` captures all command output in memory; truncation happens only afterward at line 122. | Stream output through a bounded buffer. Preserve bounded log artifacts separately, including useful head/tail diagnostics. |
| First | `agent.py:100` times out the direct shell process, allowing child processes to survive. | Start a separate process group/session, terminate the group on timeout, escalate if needed, and reap children. Use a worker cgroup/container boundary for reliable cleanup of detached processes. |
| First | `agent.py:272` parses JSON but assumes an object and valid field types. | Validate action names, argument objects, required fields, string sizes, and command types. Return a structured tool error on invalid output. |
| Next | `agent.py:338` and `agent.py:355` do not propagate exhaustion or recovered errors into a failing process exit status. | Return explicit outcomes such as completed, exhausted, interrupted, and infrastructure failure; map them to documented exit codes. |
| Next | `agent.py:204` treats every exception as a tool-parser failure. | Classify parser errors, network failures, authentication errors, context overflow, and server errors. Set explicit inference timeouts and bounded retries; remember fallback mode for the applicable session. |
| Next | `agent.py:146` budgets history by message count rather than size. | Budget prompt and response tokens, preserve complete tool exchanges, and maintain a compact investigation summary outside the transcript. |

Do not automatically execute arbitrary fenced shell examples in explanatory
responses. A structured action envelope is easier to validate. If text fallback
is retained, require a single unambiguous action and reject ambiguous responses.
LM Studio documents both native and default tool support; test the actual model
and server combination rather than assuming every API error is a grammar error:
[LM Studio tool use](https://lmstudio.ai/docs/developer/openai-compat/tools).

Make command, build/test, inference, and overall run deadlines separate settings.
The current 30-second command timeout is too restrictive for many builds and fuzz
campaigns. Add signal handling and an init process for orderly shutdown.

## 2. Provide reproducible language environments

`Dockerfile:1` currently selects Python 3.12. The image installs GCC and libc
headers, but does not configure Clang, G++, Go, Rust, the requested Python
versions, Node.js, or common project build and test tooling.

Use a shared Debian/glibc base with versioned build targets or worker images for
each language. An optional combined image can support mixed-language projects.
Keep the controller's Python environment separate from project environments.

| Project | Environment to prepare | Initial investigation tools |
| --- | --- | --- |
| C/C++ | Pinned GCC/G++ and Clang/Clang++; Make, CMake, Ninja, pkg-config, project libraries, compiler-rt, llvm-symbolizer | Both compiler builds, warnings, GCC analyzer or Clang static analysis, clang-tidy, tests with AddressSanitizer/UndefinedBehaviorSanitizer, coverage-guided fuzzing |
| Go | Pinned toolchain matching the project's `go.mod` and toolchain requirements; C compiler for cgo/race builds where needed | `go vet`, Staticcheck, `go test`, race detector on supported targets, native fuzzing, govulncheck |
| Rust | Pinned rustup toolchain, Cargo, clippy, rustfmt, native build dependencies | `cargo test --frozen`, clippy, dependency audit, cargo-fuzz for suitable targets; a separate pinned nightly image for tools that require nightly |
| Python | Separate Python 3.13 and 3.14 interpreters and virtual environments; version-specific wheels and build dependencies | pytest, Hypothesis, Ruff/security rules, type checking where configured, dependency auditing |
| Node.js | Exactly Node.js 24.22.1 and a pinned compatible package manager; project native-addon build requirements | Project tests, ESLint, TypeScript checks where applicable, generated boundary/property tests, dependency auditing |

Useful official references:

- [LLVM sanitizers](https://clang.llvm.org/docs/AddressSanitizer.html) and
  [libFuzzer](https://llvm.org/docs/LibFuzzer.html). Start sanitizer builds with
  debug information and frame pointers so failures are understandable.
- [Go vulnerability tooling](https://go.dev/doc/security/vuln/),
  [Go fuzzing/security](https://go.dev/doc/security/), and
  [Staticcheck](https://github.com/dominikh/go-tools).
- [RustSec tooling](https://rustsec.org/) for dependency advisories. This checks
  known dependency issues; it does not establish that application logic is safe.
- [uv Python installation](https://docs.astral.sh/uv/guides/install-python/) can
  prepare multiple Python versions. Pin full interpreter versions in built
  images; the 3.13/3.14 labels describe the test matrix, not reproducible pins.
- [Node.js 24.22.1 official downloads](https://nodejs.org/en/download/archive/v24.22.1)
  confirm the requested version exists. Verify release checksums/signatures and
  record the installed version in run metadata.

Set CPU, memory, PID, temporary-storage, and build parallelism limits per project.
The current 2 GiB / 2 CPU / 128 PID limits may be too small for Rust linking,
sanitizers, or substantial C++ builds. Measure a baseline build before choosing
limits. If GCC/Clang means building the compiler repositories themselves, use a
separate resource profile with substantially larger build storage and budgets.

## 3. Separate dependency preparation from offline investigation

Installing compilers alone will not make arbitrary projects build offline.
Provide a preparation command that resolves locked inputs and creates an
environment keyed by source/dependency hashes, toolchain, platform, and build
options. Run preparation in an isolated environment with controlled network
access; dependency build hooks can execute code too.

Prepare and then verify offline:

- C/C++: system development packages, third-party sources, submodules, CMake
  FetchContent inputs, and build-system downloads.
- Go: required modules and toolchains. Disable surprise toolchain downloads at
  execution time with `GOTOOLCHAIN=local`, and run against prepared modules.
  See [Go toolchain selection](https://go.dev/doc/toolchain).
- Rust: dependencies from `cargo fetch` or configured `cargo vendor`, including
  git dependencies and native inputs. Use `--frozen` for the offline run; see
  [Cargo vendoring](https://doc.rust-lang.org/cargo/commands/cargo-vendor.html).
- Python: a wheelhouse or fully prepared uv cache for each interpreter and
  platform, including build backend dependencies. Test a locked offline sync;
  see [uv caching/offline behavior](https://docs.astral.sh/uv/concepts/cache/).
- Node.js: package-manager cache matching the lockfile, native build inputs, and
  any binaries downloaded by install scripts. Prove `npm ci --offline` works in
  a fresh environment; see [npm ci](https://docs.npmjs.com/cli/v11/commands/npm-ci/).

Run an offline smoke build/test before starting model inference. Report missing
dependencies as preparation failures rather than spending model iterations on
network commands that cannot succeed.

Dependency advisory checks need a separate strategy. govulncheck, cargo-audit,
pip-audit, and npm audit are not all automatically usable offline simply because
their executables are installed. Use supported local database snapshots or run
the check during controlled preparation and import the dated result. Record
database timestamps and report unavailable checks explicitly. Do not turn on
general sandbox internet access to make an audit command pass.

## 4. Isolate the controller and verifier from project execution

Today the model's shell, agent process, and verification command share a user
and container. The read-only `agent.py` mount protects that file, but does not
create an independent execution or verification boundary. For example, a model
can edit a workspace script used by `VERIFY_CMD` or remove tests to obtain a
passing exit status.

Recommended layout:

```text
Host supervisor / trusted controller ---- restricted inference gateway --> LM Studio
                  |
                  +---- disposable language worker: build, test, fuzz
                  |     network disabled; writable project copy and scratch space
                  |
                  +---- fresh verification worker: protected checks and policy
                  |
                  +---- persistent run state, findings, logs, reproducers, patches
```

The controller owns action validation, budgets, run state, and worker lifecycle.
Keep executor operations narrowly scoped. Project workers must not receive the
Docker socket, host credentials, or access to inference administration APIs.

Mount source snapshots read-only as input, then create a disposable writable
working copy. Export proposed patches and evidence. A normal Git worktree can
carry a `.git` pointer outside the mount; use a self-contained checkout or an
explicitly controlled export when crossing the sandbox boundary.

Run protected regression checks in a fresh worker against the original revision
and any proposed patch. Keep original tests, verification scripts, and acceptance
policy outside the model's writable paths. Treat model-generated tests as useful
evidence to review, rather than the sole authority on correctness.

Add `read_only`, bounded writable temporary mounts, `init: true`, and resource
limits to appropriate services. Harden the proxy too, and pin images and Python
dependencies rather than using `latest` and an unpinned OpenAI SDK. Docker
documents these [Compose controls](https://docs.docker.com/reference/compose-file/services/)
and its [container security boundaries](https://docs.docker.com/engine/security/).

The existing socat service forwards arbitrary TCP traffic to the LM Studio port;
it does not restrict HTTP methods, endpoints, model choices, or request sizes.
An HTTP gateway can allow only required inference endpoints, cap requests and
concurrency, and keep server credentials outside the worker. In the proposed
layout, only the controller needs that gateway.

Validate network behavior on the actual WSL2/Docker setup. An internal network
alone is not evidence that only one peer/port is reachable. Add checks for the
host gateway, other containers, LAN and internet routes, DNS, IPv6, and allowed
inference endpoints. Make the LM Studio host configurable and document tested
networking modes instead of assuming `10.0.0.2` is portable.

## 5. Build a persistent investigation loop

`CONTINUOUS=1` currently reruns the same prompt with fresh message history,
including after exhausted or failed cycles. It has no durable target queue,
coverage record, finding deduplication, or resume checkpoint.

Use an explicit cycle:

1. Snapshot the source revision and load a project manifest describing language
   images, build/test commands, protected checks, scope, and budgets.
2. Establish baseline build/test/scanner results before asking the model to
   investigate. Existing failures must remain distinguishable from new ones.
3. Select a bounded target from a persistent queue: an entry point, parser,
   authorization boundary, unsafe/FFI component, failing test, or uncovered area.
4. Give the model the relevant code and diagnostics. Ask for a concrete bug
   hypothesis, necessary conditions, and a targeted investigation or test.
5. Run the investigation under fixed limits. Minimize and replay failures in a
   fresh worker; retain precise static evidence when execution is impractical.
6. Record the outcome as confirmed, suspected, rejected, duplicate, blocked, or
   no finding in the examined scope. Deduplicate with location/root-cause and
   reproducer information; a stack trace alone may be unstable.
7. Optionally produce a patch in a separate disposable copy. Verify that the
   reproducer fails before the patch and passes afterward, then run regression
   checks. Keep discovery and patch acceptance as separate decisions.
8. Persist progress and select the next target, or stop with a recorded reason.

Persist run IDs, source revisions, image digests, tool/model identifiers,
inference settings, command results, seeds/corpora, evidence, and status changes.
Use SQLite or structured JSON records plus bounded log artifacts. Keep trusted
status updates under controller control.

Combine the model with deterministic tools. Let scanners and fuzzers generate
leads, then use the LLM to explain, prioritize, design tests, and trace logic
across functions. Give each cycle a theme and track examined areas to reduce
repeated shallow reviews. One local model can perform these roles sequentially.

Bound wall time, inference usage, commands, retries, output, and disk use. Detect
repeated commands/results and stalled investigations. A clean cycle means no
finding in the examined scope, not proof that the project has no bugs.

## 6. Maintainability and operating improvements

- Split configuration, LLM transport, action parsing, execution, verification,
  persistence, and CLI behavior into small modules with explicit interfaces.
- Add meaningful harness tests using a fake LLM: invalid actions, parser
  fallback, premature completion, exhaustion, process cleanup, output flooding,
  context handling, and resume behavior. Tests must not require a GPU server.
- Add small language fixtures that prove each prepared image can build and test
  without network access. Test Python 3.13 and 3.14 separately.
- Add `.env.example` and `.dockerignore`; keep `.env`, artifacts, caches, and
  source workspaces out of Git and unnecessary Docker build context.
- Remove fixed container names for concurrent projects, and make UID/GID,
  project paths, image versions, and resource budgets configurable.
- Add inference readiness checks and distinguish a live TCP proxy from a ready
  model. Log useful failures rather than silently recovering indefinitely.
- Pin the selected model instead of choosing the first non-embedding model.
  Record model/configuration details so investigation quality is comparable.
- Update README claims about never crashing, strict timeouts, context overflow
  protection, and network isolation to match tested behavior.

## Suggested implementation order

1. Correct action parsing, completion outcomes, process cleanup, and bounded
   output; add fake-LLM regression checks and CI.
2. Add versioned language images, per-project configuration, dependency
   preparation, and offline smoke checks.
3. Separate controller/worker verification and disposable source handling.
4. Add persistent target queues, structured findings, reproducers, resume, and
   no-progress limits; then enable unattended continuous campaigns.

## Validation performed

- `docker compose config --quiet` passed using Docker Compose v5.6.1.
- Four isolated checks against the current agent confirmed that ordinary prose
  finishes a task, reasoning-only shell text is dispatched, non-object JSON
  arguments raise an exception, and a child can write a file after its parent
  shell times out. Reasoning-command execution was stubbed; the timeout check
  used a short-lived local child and a temporary directory.
- Checks used Python's standard library and a stubbed OpenAI import/response
  path. They made no inference calls and changed no project runtime files.
- No image builds, live LM Studio sessions, target-project builds, or network
  isolation tests were performed. Language/tool recommendations were checked
  against current primary documentation where linked.
