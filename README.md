# Offline Docker Sandbox for LM Studio (WSL2 + Windows)

This setup runs an autonomous local LLM agent loop inside an **offline, locked-down Docker container** in WSL2 while using **LM Studio** running natively on Windows for GPU-accelerated inference.

Stage 2 adds C/C++ (GCC and Clang), Go, Rust, Python 3.13/3.14, and Node.js
24.22.1 environments. See [language environments and offline preparation](STAGE2.md) (including dated offline dependency advisory checks)
for project manifests, preparation, smoke checks, and running a prepared project.
Stage 3 adds a [trusted host controller and protected verification](STAGE3.md),
with offline command workers and durable evidence/patch export.
Stage 4 adds [bounded investigation campaigns](STAGE4.md), with persistent target
selection, untrusted finding proposals, operator review, and reproducer replay.
Project terminology is defined in the [domain glossary](GLOSSARY.md).

---

## Architecture & How Offline Isolation Works

In Docker, setting a network to `internal: true` blocks **all** external routing—including traffic to the host machine (`127.0.0.1` or `10.0.0.2`). To give the sandbox **zero internet or LAN access** while still allowing it to talk to LM Studio, `docker-compose.yml` uses a two-container sidecar architecture:

```
┌─────────────────────────────────────────┐
│          sandbox (llm-sandbox)          │
│  • Runs agent.py as non-root (UID 1000) │
│  • Attached ONLY to `sandbox_net`       │
│  • NO internet or LAN access            │
│  • Mounts ./workspace                   │
└───────────────────┬─────────────────────┘
                    │ http://lm-proxy:1234/v1
                    ▼  (sandbox_net: internal=true)
┌─────────────────────────────────────────┐
│         lm-proxy (alpine/socat)         │
│  • Attached to `sandbox_net`            │
│    AND `host_bridge`                    │
│  • Forwards TCP 1234 -> 10.0.0.2:1234   │
└───────────────────┬─────────────────────┘
                    │ TCP 10.0.0.2:1234
                    ▼  (WSL2 mirrored network)
┌─────────────────────────────────────────┐
│       LM Studio (Windows Host)          │
│  • Serving on Local Network (:1234)     │
└─────────────────────────────────────────┘
```

### Why `10.0.0.2` Instead of `host.docker.internal`?
In WSL2 with `networkingMode=mirrored`, Windows mirrors its physical/virtual host interfaces (`127.0.0.1` and `10.0.0.2`), **not** the Linux-only `docker0` bridge (`172.17.0.1`) that `host.docker.internal` resolves to. Pointing `lm-proxy` to `10.0.0.2:1234` routes traffic directly to LM Studio on Windows.

### Sandbox Hardening
The `sandbox` service in `docker-compose.yml` enforces:
- **Network isolation:** Attached exclusively to `sandbox_net` (`internal: true`).
- **Read-only harness:** `agent.py` is mounted `:ro` so the LLM cannot modify its own control loop.
- **Dropped capabilities:** `cap_drop: [ALL]` and `security_opt: ["no-new-privileges:true"]`.
- **Resource limits:** Capped at `2.0` CPUs, `2g` RAM, and `pids_limit: 128` (prevents fork bombs).
- **Command timeouts & truncation:** Every bash command has a timeout (default `30s`) and merged stdout/stderr is drained with at most `8000` bytes retained so runaway output cannot blow up the LLM's context window.

---

## Hybrid Resilient Tool Calling (Why It Doesn't Crash)

LM Studio's OpenAI server uses a strict internal grammar parser (`peg-native`) when `tools=[...]` is passed. Many local models (including `openai/gpt-oss-20b`, Qwen, and Gemma) occasionally deviate slightly from that grammar when finishing a task, causing LM Studio to return an HTTP `400`/`500` error (`The model produced output that does not match the expected peg-native format`).

`agent.py` solves this automatically via `call_llm_resilient`:
1. **Primary Mode (Native OpenAI Tools):** Calls LM Studio with `run_bash` and `finish_task` tools for fast, structured execution.
2. **Automatic Fallback (Plain-Text Mode):** If LM Studio throws a `peg-native` parser error, `agent.py` catches it, flattens the conversation history, and immediately retries the request *without* the `tools` parameter with bounded retries.
3. **Multi-Format Parsing:** Understands native `tool_calls`, `<bash>...</bash>` and `<done>...</done>` XML tags, as single explicit actions. Prose, reasoning text, and Markdown fences are not executed.

---

## Quick Start

### 1. Prerequisites
Host-side tools (`sandbox.py`, the controller and tests) require Python 3.13 or
newer. Bundled example projects declare their minimum toolchains: Node.js
24.22.1 (`engines`), Go 1.26 (`go` directive) and Rust 1.88 (`rust-version`).
The images pin Node.js 24.22.1, Go 1.27.3 and Rust 1.99.1. Go and Cargo refuse
older toolchains; npm only warns about `engines` by default.

1. In **LM Studio (Windows)**, load a chat/coding model and enable **Serve on Local Network** on port `1234`.
2. Create your settings file and set `LM_STUDIO_HOST` and `LLM_MODEL` (both are required):
   ```bash
   cp .env.example .env
   ```
3. Build the sandbox image once in WSL2:
   ```bash
   docker compose build
   ```

### 2. Interactive Mode
Opens a prompt where you can type tasks one after another:
```bash
docker compose run --rm sandbox
```

### 3. Single-Task Mode
Pass the task directly on the command line; the container runs until the task completes and then exits:
```bash
docker compose run --rm sandbox "Create a Python script that generates the first 50 prime numbers and save them to primes.txt"
```

### 4. Choosing a Specific Model
`agent.py` requires an explicit model and no longer picks one from LM Studio. Set `LLM_MODEL`:
```bash
LLM_MODEL="qwen/qwen2.5-coder-14b" docker compose run --rm sandbox "Refactor the scripts in this folder"
```

---

## Getting Results In and Out of the Sandbox

The container's `/home/agent/workspace` directory is bind-mounted directly to `./workspace` on your WSL2 host:
```yaml
volumes:
  - ./workspace:/home/agent/workspace
```
- **Extracting outputs:** Any file the LLM writes inside `/home/agent/workspace` (e.g., `workspace/hello.sh`, reports, generated code, SQLite databases) is immediately available in `~/util/sandbox/workspace/` on your host machine.
- **Providing inputs:** Drop source code, datasets, or prompt instructions into `~/util/sandbox/workspace/` on your host machine and they are immediately accessible to the LLM inside `/home/agent/workspace`.

---

## Stopping Conditions: How Does the Agent Know When to Stop?

You have two ways to control when the loop stops:

### 1. LLM Self-Reporting (Default)
The LLM inspects the exit codes and `stdout`/`stderr` of its commands and calls `finish_task` (or outputs `<done>...</done>`) when it believes the goal is satisfied.

### 2. Deterministic Verification Gate (`VERIFY_CMD`)
Local LLMs can sometimes declare victory prematurely before tests pass. Setting `VERIFY_CMD` enforces an external bash check inside the sandbox **every time** the LLM tries to finish:
- If `VERIFY_CMD` exits with code `0`, the loop accepts completion and stops.
- If `VERIFY_CMD` exits with a non-zero code, the harness **rejects** completion, feeds the test/check failure output back to the LLM, and forces it to keep iterating until the check passes.

**Example — Run until `pytest` passes:**
```bash
VERIFY_CMD="pytest -q" docker compose run --rm sandbox \
  "Write a calculator module in calc.py and a test suite in test_calc.py"
```

**Example — Run until an output file exists and has at least 10 lines:**
```bash
VERIFY_CMD="test -f results.txt && [ \$(wc -l < results.txt) -ge 10 ]" \
  docker compose run --rm sandbox \
  "Generate 10 unique system administration tips into results.txt"
```

---

## Running in an Infinite Loop

There are two ways to run the sandbox in an infinite loop depending on your goal:

### Mode A: Infinite Steps Until Task Completes (`MAX_ITERATIONS=0`)
By default, a task stops after `20` steps (`MAX_ITERATIONS=20`). Setting `MAX_ITERATIONS=0` removes the step limit so the agent keeps working indefinitely until the task (and optional `VERIFY_CMD`) succeeds:

```bash
MAX_ITERATIONS=0 VERIFY_CMD="./run_tests.sh" docker compose run --rm sandbox \
  "Fix all bugs in app.py until ./run_tests.sh passes"
```

> **Context Window Protection:** To reduce history growth, `agent.py` automatically trims the message history using a sliding window (`MAX_HISTORY_MESSAGES=24`) and an estimated token budget (`MAX_CONTEXT_TOKENS=12000`, about 3 characters per token), preserving the system prompt, original task, and recent complete exchanges. Steps that leave the window are condensed by the harness (not the model) into a bounded record of each command, its exit status and first/last output lines (`MAX_SUMMARY_CHARS=3000`, `0` disables), which is shown with the task on every later turn; excerpts are marked as untrusted program output, and older entries are dropped with a count once the record is full. The estimate is approximate and does not count the tool schema, and the newest message is always kept, so set the budget below your model's loaded context length with margin.

### Mode B: Continuous Autonomous Loop (`CONTINUOUS=1`)
If you want the agent to run **forever** in a loop—restarting with a fresh context window after each completed cycle (useful for fuzzing, continuous code improvement, generating synthetic data, or polling a task queue in `./workspace`)—set `CONTINUOUS=1`:

```bash
CONTINUOUS=1 MAX_ITERATIONS=30 docker compose run --rm sandbox \
  "Read TODO.md in the workspace, pick the next unfinished item, implement and test it, mark it done in TODO.md, and finish."
```

To run it in the background as a daemon and watch its logs:
```bash
CONTINUOUS=1 docker compose run -d --name agent-daemon sandbox \
  "Continuously generate new unit test edge cases in test_suite.py and verify they pass."

# Watch live logs
docker logs -f agent-daemon

# Stop the infinite daemon when done
docker stop agent-daemon && docker rm agent-daemon
```

---

## Configuration Reference (Environment Variables)

All settings can be passed on the command line or configured in a `.env` file next to `docker-compose.yml` (copy `.env.example` to start):

| Variable | Default | Description |
| :--- | :--- | :--- |
| `LM_STUDIO_HOST` | *(required)* | IP/hostname used by `lm-proxy` to reach LM Studio on Windows; `.env.example` uses the tested `10.0.0.2` |
| `LM_STUDIO_PORT` | `1234` | Port where LM Studio's Local Inference Server is listening |
| `LLM_MODEL` | *(required)* | Model ID in LM Studio (e.g., `openai/gpt-oss-20b`, `qwen/qwen2.5-coder-14b`) |
| `MAX_ITERATIONS` | `20` | Maximum steps per task (`0` = infinite steps) |
| `MAX_HISTORY_MESSAGES` | `24` | Sliding window size in messages (`0` disables) |
| `MAX_CONTEXT_TOKENS` | `12000` | Estimated prompt-token budget for retained history (`0` disables) |
| `MAX_SUMMARY_CHARS` | `3000` | Cap on the condensed record of trimmed-away steps (`0` disables) |
| `COMMAND_TIMEOUT` | `30` | Maximum seconds a single bash command can run before being killed |
| `VERIFY_CMD` | *(empty)* | Optional bash command that must exit `0` before the agent is allowed to finish |
| `CONTINUOUS` | `0` | Set to `1` to restart the task in an infinite cycle (`while True`) |


## Stage 1: harness reliability

Run the regression checks without installing an SDK or starting LM Studio:

```bash
python3 -m unittest discover -s tests -v
```

The SonarQube workflow (`.github/workflows/sonarqube.yml`) builds and prepares
the `python313` profile, runs the full suite including Docker boundary tests
under `coverage`, and uploads `coverage.xml` to SonarQube Cloud. To reproduce
the coverage report locally (coverage settings live in `pyproject.toml`):

```bash
python3 -m pip install --only-binary :all: coverage==7.16.2
python3 sandbox.py build python313
python3 sandbox.py prepare examples/python313.json --state artifacts/python313.json
SANDBOX_DOCKER_TESTS=1 SANDBOX_TEST_PROFILE=python313 python3 -m coverage run -m unittest discover -s tests
python3 -m coverage xml
python3 -m coverage report
```

Completion now requires a valid `finish_task` call or a single `<done>` action.
Text fallback commands must be a single `<bash>` action. Invalid tool arguments
are returned to the model as errors. Reasoning content and fenced examples are
never executed. Parser fallback is limited to errors mentioning `peg-native`;
other inference errors fail the run after the SDK's bounded retries.

Commands drain merged stdout/stderr into a bounded buffer and kill their process
group when they finish, time out, or are interrupted. Background work cannot be
kept between commands. Programs deliberately detaching into a new session still
require the planned worker-container boundary for complete cleanup.

Single-task exit codes are `0` completed, `1` failed, `2` iteration limit, and
`130` interrupted. Continuous mode proceeds only after a completed cycle; failed
or exhausted cycles stop with a nonzero exit code. `LLM_TIMEOUT` defaults to 120
seconds per API request (with at most two SDK retries). `WORKSPACE` can override
the working directory when running the harness directly outside Compose.

`VERIFY_CMD` still runs in the agent's container and can reference writable
project files. Independent protected verification, language images, offline
preparation, and persistent investigation state remain later stages; see
[the implementation proposal](IMPROVEMENTS.md) and [progress](AGENTS.md).

## License

This project is released under the [MIT License](LICENSE).
