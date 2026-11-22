# Agent Sandbox

Offline, hardened Docker sandbox and host controller for running local-LLM code
and security investigations across C/C++, Go, Rust, Python, and Node.js.

It works with any OpenAI-compatible inference server (such as LM Studio,
Ollama, llama.cpp, or vLLM) across Linux, macOS, and Windows/WSL2.

The repository supports two workflows:

1. **Trusted Host Controller & Offline Workers (`sandbox.py`, recommended):**
   The host controller makes inference requests, executes each model command in
   a fresh `--network none` worker container with a read-only root filesystem,
   gates completion on operator-owned protected verification, exports
   Git-compatible patches and evidence artifacts, and supports bounded
   multi-target campaigns and offline dependency advisory scans.
2. **Legacy Docker Compose Mode (`docker-compose.yml`):**
   Runs `agent.py` inside a workspace container attached to an internal-only
   Docker network alongside an `llm-proxy` sidecar. Useful for quick interactive
   sessions in `./workspace`, though with weaker boundaries than `sandbox.py`
   (see [SECURITY.md](SECURITY.md)).

Trust boundaries and security invariants are documented in
[SECURITY.md](SECURITY.md), and contributor/roadmap notes in
[AGENTS.md](AGENTS.md).

---

## Prerequisites

- **Docker** with BuildKit and Compose `2.24.4+`
- **Python 3.13+** on the host (for `sandbox.py`, controller, and tests)
- **Host controller dependencies** (required for `sandbox.py run` and
  `sandbox.py campaign resume`):
  ```bash
  python3 -m venv .venv
  .venv/bin/python -m pip install --require-hashes -r requirements-controller.txt
  ```
- **Optional:** [OSV-Scanner](https://google.github.io/osv-scanner/) `2.x` on
  `PATH` for offline dependency advisory scanning (`sandbox.py advisories scan`)
- An OpenAI-compatible inference server serving a local chat/coding model

### Supported Language Profiles & Minimum Runtimes

`docker/images.json` and `Dockerfile` pin six language worker profiles:

| Profile | Pinned Toolchain | Pre-installed Offline Analysis Tools |
| :--- | :--- | :--- |
| `cpp` | GCC 12, Clang 19 (Debian Trixie) | `clang-tidy-19`, ASan/UBSan, libFuzzer |
| `go` | Go 1.27.3 | `Staticcheck v0.8.2`, `go vet`, `go test -fuzz` |
| `rust` | Rust 1.99.1 | `clippy`, `rustfmt` |
| `python313` | Python 3.13.18 | `Ruff 0.17.1`, `Hypothesis 6.170.1` |
| `python314` | Python 3.14.10 | `Ruff 0.17.1`, `Hypothesis 6.170.1` |
| `node` | Node.js 24.22.1 | `ESLint 10.13.0` |

Bundled example projects declare minimum toolchains: Node.js `>=24.22.1`
(`engines`), Go `1.26` (`go` directive), and Rust `1.88` (`rust-version`). Go
and Cargo refuse older toolchains; npm only warns about `engines` by default.

> **Not installed for offline use:** `govulncheck`, `cargo-audit`, and
> `pip-audit` require separate ecosystem-specific offline mirrors (use
> `sandbox.py advisories` instead); `cargo-fuzz` requires nightly Rust.

---

## 1. Language Environments & Offline Preparation

`sandbox.py` builds language worker images, snapshots project source, runs
dependency preparation while networked, and verifies that the project builds and
passes its baseline checks with `--network none` before saving a prepared state
JSON.

### Try a Bundled Fixture

```bash
python3 sandbox.py build python313
python3 sandbox.py prepare examples/python313.json --state artifacts/python313.json
python3 sandbox.py check artifacts/python313.json
```

Repeat with `cpp`, `go`, `rust`, `python314`, or `node` and the matching file in
`examples/`. Each `build` probes the toolchain with networking disabled. `check`
replays the prepared image snapshot rather than later edits in your host checkout;
re-run `prepare` after updating source or verification checks.

### Configure a Target Project Manifest

Copy an example manifest and set `source` and `verification.source` relative to
the manifest's directory:

```json
{
  "name": "my-python-project",
  "source": "../../my-project",
  "profile": "python314",
  "prepare": [
    "python3 -m venv .venv",
    ".venv/bin/python -m pip install --require-hashes -r requirements.lock"
  ],
  "check": ".venv/bin/python -m pytest -q",
  "verification": {
    "source": "verification/my-project",
    "command": "python3 -I /opt/sandbox/verification/check.py python3 -I /opt/sandbox/verification/harness.py"
  },
  "memory": "4g",
  "cpus": 2,
  "pids": 256,
  "timeout": 600,
  "prepare_timeout": 1800
}
```

- `prepare` commands run in separate `bash -e` processes. Files persist between
  steps; shell variables and activated environments do not (use absolute or
  workspace-relative executable paths). The whole preparation build is limited to
  `prepare_timeout` seconds (default `1800`); on expiry the build is cancelled and
  no state is written. BuildKit does not apply CPU or memory limits to build steps,
  so set builder-side limits separately if untrusted preparation could exhaust them.
- Typical per-language offline prep:
  - **Go:** `go mod download`; offline check `GOPROXY=off go test -mod=readonly ./...` (`GOTOOLCHAIN=local` is set in the image).
  - **Rust:** commit `Cargo.lock`, run `cargo fetch --locked`; check `cargo test --frozen`.
  - **Node:** commit `package-lock.json`, run `npm ci`; test `npm ci --offline` if lock reconstruction is needed.
  - **Python:** create `.venv` and install pinned/hashed dependencies including test runners and build backends.
  - **C/C++:** fetch third-party inputs, submodules, or CMake downloads during `prepare` so offline builds stay local.
- **Snapshot exclusions:** `.git`, `.env*`, common credential files/directories
  (`.ssh`, `.aws`, `.npmrc`, `.pypirc`, `.netrc`, `.codex`, `.agents`), and build
  caches and outputs (`__pycache__`, `.venv`, `node_modules`, `target`, `artifacts`,
  `.pytest_cache`, `build`, `dist`) are excluded. Preparation and run snapshots use
  the same list (`SKIP` in `docker/transfer.py`), so a project that keeps tracked
  source under `build/` or `dist/` must rename it. Preparation copies the source
  without following links and rejects every symlink and special file, because run
  snapshots cannot represent them. Always inspect the target source
  directory first; exclusions do not detect secrets embedded in source files.
  Prepared images are local artifacts containing source and dependencies—do not
  publish them without review.
- **Managed proxy CAs:** pass `--ca /path/to/combined-ca.pem` to `build` or
  `prepare` to mount an extra CA trust bundle as a BuildKit secret (it is never
  copied into the image).
- **Build proxies:** `build` and `prepare` override every proxy variable Docker
  would inject from its client configuration (`~/.docker/config.json`), because
  build steps run project code that could read embedded credentials. Pass
  `--proxy http://proxy.internal:3128` to use a proxy for build steps; URLs with
  credentials are rejected. Workers and checks always run with all proxy variables
  cleared and with Docker logging disabled (`--log-driver none`); the controller
  keeps its own bounded copy of command output.
- **C++ ASan/PIE note:** In some high-entropy ASLR environments, older ASan PIE
  executables can hit an allocator/ASLR address conflict (see LLVM
  [D147984](https://reviews.llvm.org/D147984)). The bundled C++ fixture passes
  `-fno-pie -no-pie` on its sanitizer check binary; for real projects requiring
  PIE, prefer a toolchain/runtime with the allocator fix.

### Protected Verification

The optional (but required for `sandbox.py run`) `verification` section copies
operator-owned checks into `/opt/sandbox/verification` as root-owned files inside
the image. During verification, the root filesystem and `/home/agent` workspace
are read-only; emit build artifacts and caches to `/tmp` (`HOME`, `GOCACHE`, and
`CARGO_TARGET_DIR` point to `/tmp`, while `GOPATH` and `CARGO_HOME` retain
prepared dependencies).

Preparation requires both the workspace `check` and protected `verification`
baselines to pass before saving the state JSON. Keep critical assertions in the
protected verification directory so edits to mutable workspace tests cannot
falsely satisfy completion.

**Never decide the verdict in a process that runs candidate code.** If candidate
code is imported or called by the process whose exit status is the verdict, the
candidate can exit with status `0` before the assertions run. The bundled
examples share one verification directory (`examples/verification`): a protected
supervisor (`check.py`) runs a language-specific harness (`harness.*`) as a child
process. The harness reads test cases on stdin and prints `answer: <value>` lines;
the supervisor reads its output incrementally, keeps only bounded parts, compares
every answer itself and passes only when all expected answers arrive,
so an early exit, crash, or wrong answer fails. Build steps may compile candidate
code, but only the supervisor's exit status should decide the outcome:

```bash
python3 -I /opt/sandbox/verification/check.py python3 -I /opt/sandbox/verification/harness.py
```

### Offline Dependency Advisory Checks

Advisory snapshots are fetched while networked and scanned offline on the host
(snapshots are never baked into images or exposed to workers):

```bash
python3 sandbox.py advisories fetch artifacts/advisories
python3 sandbox.py advisories fetch artifacts/advisories --ecosystem Go --ecosystem crates.io
python3 sandbox.py advisories scan artifacts/go.json artifacts/advisories \
  --output artifacts/go-advisories.json
```

`fetch` downloads OSV ecosystem databases (`Go`, `PyPI`, `crates.io`, `npm`),
replaces existing databases only after every requested download validates, and
writes SHA-256/size/timestamp metadata last. Each database records its own fetch
time, so refreshing one ecosystem does not renew the others. `scan` extracts
recognized lockfiles (`go.mod`, `Cargo.lock`, `package-lock.json`,
`requirements*.txt`, `poetry.lock`, `uv.lock`, `Pipfile.lock`) from the prepared
image in a read-only, resource-limited container with networking disabled,
verifies snapshot hashes and the age of each database it uses against
`--max-age-days` (default `7`), and runs
`osv-scanner --offline --offline-vulnerabilities`. Discovery keeps at most `200`
lockfiles of up to `5 MiB` each (`64 MiB` in total) and lists every recognized
lockfile it leaves out.

Exit codes: `0` clean, `1` known advisories found, `2` check incomplete
(missing database, missing or omitted lockfiles, discovery errors,
tampered/stale snapshot, a database fetched before per-database times were
recorded, or scanner failure—never reported as clean). Matching lockfile versions to OSV advisories
does not analyze reachability or establish that code is safe.

---

## 2. Isolated Investigation Runs (`sandbox.py run`)

```bash
.venv/bin/python sandbox.py run artifacts/python313.json \
  "Investigate one area and retain a reproducer or precise evidence." \
  --model your-model-id --steps 20 --timeout 1800
```

- **Command timeout:** each model command may run for the manifest `timeout`
  (default `600` seconds), bounded by the remaining run time. Override it with
  `--command-timeout SECONDS` on `run` or `campaign resume`. (`COMMAND_TIMEOUT`
  applies only to legacy Compose.)
- **Endpoint & model selection:** The controller connects to `--base-url` or
  `OPENAI_BASE_URL`, defaulting to `http://127.0.0.1:1234/v1`. Model selection
  is required (`--model` or `LLM_MODEL`). Pass `OPENAI_API_KEY` if your server
  requires authentication (consumed only on the host, never passed to workers).
- **Resilient tool calling:** `agent.py` uses native OpenAI tool calling
  (`run_bash`, `finish_task`) first. If a server returns a `peg-native` grammar
  parser error (seen with some local models in LM Studio), it falls back to
  single explicit XML actions (`<bash>...</bash>`, `<done>...</done>`) with
  bounded retries. Pass `--text-actions` to skip native tools from the start.
  Ordinary prose, reasoning text, and Markdown code fences are never executed.
  Only the first tool call in a response is executed, so `--steps` bounds the
  number of commands; the model is told that further calls were ignored.
- **Worker isolation:** Each model command runs in a fresh container with
  `--network none`, dropped capabilities, `no-new-privileges:true`, a read-only
  root filesystem, executable `/tmp` tmpfs, CPU/memory/PID limits from the
  manifest, and a run-specific home volume removed at the end of the run.
  Container cleanup also kills detached background descendants on exit, timeout,
  or interrupt.
- **Protected acceptance:** Completion is accepted only after an explicit
  `finish_task`/`<done>` action **and** a passing protected verification run in a
  separate fresh verifier container against pristine dependencies and read-only
  candidate source.

### Retained Run Artifacts & Patch Review

Each run writes controller-owned artifacts under `artifacts/runs/<run-id>/`
(override with `--artifacts`):

- `input.json` and `result.json`: prepared identity, task, configuration,
  model, endpoint, outcome, and baseline/candidate verification states.
- `events.jsonl`: validated command results and assistant action messages.
- `controller.log`: bounded execution and loop diagnostics.
- `original.json` and `candidate.json`: regular-file source snapshots
  (transfer limits: regular files only, max `5,000` files / `16 MiB`; symlinks,
  special files, caches, and credential paths are excluded or rejected).
- `candidate.patch` and `changes.json`: Git-compatible text/binary/mode diff and
  changed paths.

Put reproducers or notes you want retained in a regular directory such as
`evidence/`. Candidate outputs are untrusted evidence and are never applied
automatically. To independently replay a candidate:

```bash
python3 sandbox.py verify artifacts/python313.json \
  --candidate artifacts/runs/RUN_ID/candidate.json
```

`candidate.patch` is a diff against the prepared original snapshot
(`original.json`), which includes files that `prepare` generated or changed (such
as lockfiles). It may not apply to a plain repository checkout. To test it, run
the check from a working tree whose contents match `original.json`, for example
a copy restored from that snapshot (compare against the recorded `source_hash`):

```bash
git apply --check /path/to/artifacts/runs/RUN_ID/candidate.patch
```

---

## 3. Bounded Investigation Campaigns (`sandbox.py campaign`)

Campaigns manage persistent target queues, retry/stall limits, untrusted
finding proposals, operator review, and fresh-worker reproducer replay.

Create an operator-authored `targets.json` (array of task strings or objects):

```json
[
  {"task": "Investigate length handling", "scope": "src/parser.c:decode", "priority": 10},
  {"task": "Investigate error cleanup", "scope": "src/io.c", "priority": 0}
]
```

Create, run, and inspect a campaign:

```bash
python3 sandbox.py campaign create artifacts/my-campaign artifacts/my-project.json targets.json
.venv/bin/python sandbox.py campaign resume artifacts/my-campaign \
  --max-runs 3 --steps 20 --timeout 1800 --budget 3600 \
  --continue-on-failure --model your-model-id
python3 sandbox.py campaign status artifacts/my-campaign
```

- **Scheduling & budgets:** Targets are ordered by fewest attempts, descending
  `priority`, and input order. Duplicate `(task, scope)` pairs are queued once.
  `--max-attempts` defaults to `3`; `--stall-limit` defaults to `2` (the
  minimum) successive unsuccessful attempts with unchanged candidate snapshots
  (excluding report wording), and `3` identical command/result digests inside a
  run also mark a target stalled. The next target is chosen again after every
  attempt, so with `--continue-on-failure` a target can be retried within one
  `resume` while `--max-runs` allows it. An interrupted run (Ctrl-C or SIGTERM)
  always stops the campaign with exit code `130`.
- **State integrity & outcomes:** Writes are atomic and locked (`flock`). Version
  2 campaigns pin the canonical SHA-256 of `prepared.json` and reject state
  changes. `campaign resume` exits `0` only on `all_completed`; other stop
  reasons (`run_limit`, `time_budget`, `attempt_*`, `interrupted`,
  `no_eligible_targets`) exit `1`. Clean completion or a `no_finding` report is
  not a security verdict.

### Finding Proposals, Review, and Reproducer Replay

Campaign prompts instruct the agent to write `investigation-findings.json`
(capped at `64 KiB` and `100` findings, with `disposition` in
`finding | no_finding | blocked`). After each run, the controller imports the
report into `proposals.json` with forced `suspected` status and controller-owned
evidence references, grouping identical `(location, root_cause)` pairs without
trusting model-supplied statuses or host paths.

Record an operator review decision (appends to `findings.json`) or an operator
finding manually:

```bash
python3 sandbox.py campaign review artifacts/my-campaign 0 0 0 --status confirmed
python3 sandbox.py campaign finding artifacts/my-campaign finding.json
```

Supported statuses: `confirmed`, `suspected`, `rejected`, `blocked`,
`no_finding`. Manual `finding.json` files must link existing relative paths under
the campaign's `runs/` directory.

To replay an operator-owned reproducer script against both `original.json` and
`candidate.json` in separate fresh offline workers:

```bash
python3 sandbox.py campaign replay artifacts/my-campaign runs/0/ATTEMPT/RUN reproducer.sh --timeout 600
```

Reports saved under `runs/replays/` record command text, snapshot hashes, exit
codes, and bounded diagnostics (`failure_then_pass` marks a nonzero non-timeout
original exit followed by exit `0` on candidate).

---

## 4. Legacy Docker Compose Mode

For quick single-container sessions backed by a host `./workspace` bind mount,
`docker-compose.yml` uses a two-container sidecar architecture so the sandbox
has zero internet or LAN access while still reaching your local inference
server. The proxy is limited to 16 concurrent connections and small CPU, memory
and PID limits. Because one container serves every action, the loop kills all
processes left by an earlier action (`SANDBOX_REAP_STRAYS=1`) before and after
each command:

```text
┌─────────────────────────────────────────┐
│          sandbox (non-root UID 1000)    │
│  • Runs agent.py inside container       │
│  • Attached ONLY to `sandbox_net`       │
│  • Mounts ./workspace (rw), agent.py(ro)│
└───────────────────┬─────────────────────┘
                    │ http://llm-proxy:1234/v1
                    ▼  (sandbox_net: internal=true)
┌─────────────────────────────────────────┐
│         llm-proxy (alpine/socat)        │
│  • Attached to `sandbox_net`            │
│    AND `host_bridge`                    │
│  • Forwards TCP 1234 -> LLM_HOST:PORT   │
└───────────────────┬─────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│   OpenAI-compatible inference server    │
│   (e.g., LM Studio / Ollama / llama.cpp)│
└─────────────────────────────────────────┘
```

### Quick Start (Compose)

1. Start your OpenAI-compatible inference server on the host (listening on port
   `1234` by default, bound to an address reachable from Docker).
2. Create `.env` from `.env.example` and set `LLM_HOST` and `LLM_MODEL`:
   ```bash
   cp .env.example .env
   ```
   - `LLM_HOST=host.docker.internal` works out of the box on Docker Desktop
     (macOS/Windows) and Linux Docker Engine (via `extra_hosts: host-gateway`).
   - In mirrored-network setups (such as WSL2 `networkingMode=mirrored`), set
     `LLM_HOST` to your host or mirrored gateway IP if `host.docker.internal`
     is unreachable.
3. Build and run:
   ```bash
   docker compose build
   docker compose run --rm sandbox                          # interactive prompt
   docker compose run --rm sandbox "Create hello.py"        # single task
   LLM_MODEL="your-model-id" docker compose run --rm sandbox "Refactor app.py"
   ```

Files created inside `/home/agent/workspace` appear in `./workspace/` on the
host. (For a legacy Compose run against a prepared image where edits are
discarded on exit, combine `docker-compose.yml` with
`docker-compose.prepared.yml` via `PREPARED_IMAGE`.)

### Compose Stopping Modes & Context Protection

- **Verification gate (`VERIFY_CMD`):** Runs inside the `sandbox` container when
  the agent tries to finish; nonzero exit rejects completion and returns
  output to the model.
- **Unbounded step count (`MAX_ITERATIONS=0`):** Removes the default `20`-step
  limit until completion or `VERIFY_CMD` succeeds.
- **Continuous loop (`CONTINUOUS=1`):** Restarts with a fresh context after each
  completed cycle (stops on failed or exhausted cycles).
- **Context window protection:** `agent.py` trims history using a sliding
  message window (`MAX_HISTORY_MESSAGES=24`) and an estimated token budget
  (`MAX_CONTEXT_TOKENS=12000`, ~3 chars/token), keeping complete tool exchanges.
  Trimmed steps are condensed deterministically by the harness into a bounded
  record (`MAX_SUMMARY_CHARS=3000`, `0` disables) labeled as untrusted program
  output and appended to the task prompt view. Set `MAX_CONTEXT_TOKENS` below
  your loaded model context length with margin for tool schemas.

### Configuration Reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| `LLM_HOST` | *(required in Compose)* | Host/IP used by `llm-proxy` (falls back to `LM_STUDIO_HOST` if set) |
| `LLM_PORT` | `1234` | Port of the inference server (falls back to `LM_STUDIO_PORT` if set) |
| `LLM_MODEL` | *(required)* | Model ID served by the inference endpoint |
| `OPENAI_BASE_URL` | `http://127.0.0.1:1234/v1` | Host controller inference base URL (same as `--base-url`). Compose always uses `http://llm-proxy:1234/v1` and ignores this variable |
| `OPENAI_API_KEY` | `local` | Host controller API key, read from the host shell. Compose always passes the dummy `local` and never forwards real credentials into the sandbox, so use the host controller for servers that require authentication |
| `MAX_ITERATIONS` | `20` | Max steps per task (`0` = unlimited) |
| `MAX_HISTORY_MESSAGES` | `24` | Sliding window size in messages (`0` disables) |
| `MAX_CONTEXT_TOKENS` | `12000` | Estimated prompt-token budget (`0` disables) |
| `MAX_SUMMARY_CHARS` | `3000` | Condensed record cap for trimmed steps (`0` disables) |
| `COMMAND_TIMEOUT` | `30` | Legacy Compose timeout in seconds per shell command (`agent.py`); `sandbox.py run` uses the manifest `timeout` or `--command-timeout` |
| `LLM_TIMEOUT` | `120` | Timeout in seconds per inference API call |
| `VERIFY_CMD` | *(empty)* | Legacy Compose bash check required before completion |
| `CONTINUOUS` | `0` | Set to `1` to repeat completed cycles (`agent.py`) |
| `WORKSPACE` | `/home/agent/workspace` | Working directory override when running `agent.py` directly |

Single-task exit codes for `agent.py`: `0` completed, `1` failed, `2` iteration
limit exhausted, `130` interrupted.

---

## Testing & Coverage

Run the standard-library unit and subprocess regression suite (requires no
inference server or Docker):

```bash
python3 -m unittest discover -s tests -v
```

Run the full suite including Docker isolation boundary tests and coverage
reporting (matches `.github/workflows/sonarqube.yml`, using `coverage` settings
in `pyproject.toml`):

```bash
python3 -m pip install --only-binary :all: coverage==7.16.2
python3 sandbox.py build python313
python3 sandbox.py prepare examples/python313.json --state artifacts/python313.json
SANDBOX_DOCKER_TESTS=1 SANDBOX_TEST_PROFILE=python313 python3 -m coverage run -m unittest discover -s tests
python3 -m coverage xml
python3 -m coverage report
```

To run all six language baseline and sanitizer integration checks:

```bash
for profile in cpp go rust python313 python314 node; do
  python3 sandbox.py build "$profile"
  python3 sandbox.py prepare "examples/${profile}.json" --state "artifacts/${profile}.json"
done
SANDBOX_DOCKER_TESTS=1 python3 -m unittest discover -s tests -v
```

---

## License

This project is released under the [MIT License](LICENSE).
