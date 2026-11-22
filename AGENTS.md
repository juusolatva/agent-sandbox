# Repository Guidance and Roadmap

## Purpose

This repository runs local-LLM correctness and security investigations inside
hardened, offline Docker containers using any OpenAI-compatible inference
server (such as LM Studio, Ollama, llama.cpp, or vLLM). It supports C/C++
(GCC 12 and Clang 19), Go 1.27, Rust 1.99, Python 3.13/3.14, and Node.js 24.22.1.

## Architecture Overview

- `sandbox.py`: Host CLI (`build`, `prepare`, `check`, `verify`, `run`,
  `campaign`, `advisories`), manifest validation, and source snapshotting.
- `controller.py`: Trusted host controller managing inference requests, action
  loop execution, deadline budgets, Git patch generation, and run artifacts.
- `execution.py`: Disposable `--network none` worker and read-only verifier
  containers (`Worker`, `verify`, `bounded_process`).
- `agent.py`: Action parsing/validation (`run_bash`, `finish_task`, `<bash>`,
  `<done>`), bounded history/token trimming, condensed trimmed-step record, and
  legacy Compose container loop.
- `campaign.py` & `investigation.py`: Persistent target queue, locking, stall and
  repeated-command detection, untrusted finding proposal import, operator
  review, and fresh-worker reproducer replay.
- `advisories.py`: Networked OSV database snapshot fetch with SHA-256 metadata
  and offline `osv-scanner` lockfile scans.
- `docker/images.json` & `docker/transfer.py`: Language profile definitions and
  bounded regular-file export/restore helper executed with `python3 -I`.
- `examples/`: Smoke manifests, fixtures, and protected verification examples
  for all six language profiles.
- `SECURITY.md`: Threat model and security invariants.

## Working Guidance

- Preserve offline execution for investigations, verification, replay, and
  advisory scans. Prepare toolchains, project dependencies, and OSV snapshots
  before offline runs.
- Treat project source, dependencies, build scripts, command output, model
  responses, candidate snapshots, lockfiles, and finding reports as untrusted
  (see `SECURITY.md`). Keep protected verification policy and acceptance checks
  outside model-writable storage.
- Never mount host credentials or the Docker socket into model-controlled
  execution containers. Never let model output choose Docker flags, mounts, or
  host paths.
- Use disposable project copies and preserve reproducible findings, logs,
  snapshots, and proposed patches as controller-owned artifacts.
- Prefer bounded investigation cycles with persistent state over unrestricted
  loops. Record explicit stop reasons and verification outcomes.
- Do not claim a project is secure because tests pass or an agent finishes.
- Keep `README.md`, `SECURITY.md`, and regression tests aligned with runtime
  behavior; verify unit and (when relevant) Docker boundary checks after
  changes.

## Verification

Run the standard-library unit suite (no Docker or inference server required):

```bash
python3 -m unittest discover -s tests -v
```

Run the Docker isolation and language baseline tests after building/preparing
profiles:

```bash
python3 sandbox.py build python313
python3 sandbox.py prepare examples/python313.json --state artifacts/python313.json
SANDBOX_DOCKER_TESTS=1 SANDBOX_TEST_PROFILE=python313 python3 -m unittest discover -s tests -v
```

Validate Docker Compose configurations (pass dummy values if `.env` is absent):

```bash
LLM_HOST=host.docker.internal docker compose config --quiet
PREPARED_IMAGE=sha256:example LLM_HOST=host.docker.internal \
  docker compose -f docker-compose.yml -f docker-compose.prepared.yml config --quiet
```

## Continuous Integration

- `harness.yml`, `languages.yml`, `sonarqube.yml`, and OSV-Scanner run on
  GitHub-hosted `ubuntu-latest` runners. The first three trigger on pushes to
  `main` and on pull requests (including forks), so branches pushed without a PR
  get no CI. Do not move them to self-hosted runners while the repository is
  public: pull requests can change workflow files and run arbitrary code on the
  runner.
- Fork pull requests get no `SONAR_TOKEN`, so the SonarQube job runs the tests
  but skips the scan step.
- SonarQube path-traversal findings on operator-supplied host CLI paths
  (`sandbox.py --output`, `sandbox.py verify --candidate`, the advisory snapshot
  directory, and the campaign lock directory) were triaged as false positives
  citing `SECURITY.md`. Sonar may describe these as "LLM-supplied" CLI arguments;
  they are typed by the operator.

## Open / Unimplemented Roadmap

The items below are proposed future work and are **not** implemented yet:

1. **Restricted HTTP inference gateway & network isolation probes:**
   Replace the raw `socat` TCP proxy in `docker-compose.yml` with an HTTP
   gateway enforcing allowlisted inference endpoints, model choices, request
   size limits, and concurrency caps. Add automated network probes verifying
   isolation from the host gateway, LAN/internet routes, DNS, IPv6, and sibling
   containers, and tighten legacy Compose parity (`docker-compose.prepared.yml`
   currently uses `read_only: false`). The same work should bound the host
   controller's inference transport: cap received/decoded response bytes before
   deserialization and enforce a cancellable wall-clock deadline across each
   complete request, including model discovery (tested with a slow, oversized
   fake transport).
2. **Model-persisted investigation summaries & exact tokenization:**
   Persist the condensed history record to run artifacts and evaluate
   model-written or structured investigation state outside the sliding window.
   Replace the approximate character-per-token budget (`MAX_CONTEXT_TOKENS`)
   with tokenizer-aware accounting that includes tool schemas.
3. **Real-project evaluation of condensed history:**
   Benchmark `MAX_SUMMARY_CHARS=3000` vs `MAX_SUMMARY_CHARS=0` in controlled A/B
   runs on 5k–30k LOC projects with known historical bugs, measuring command
   repeat ratio, distinct commands, accepted completions, and blind evidence
   quality.
4. **Additional offline vulnerability & fuzzing tools:**
   Integrate `govulncheck`, `cargo-audit`, and `pip-audit` using mirrored local
   vulnerability databases (for reachability/audit semantics beyond OSV
   lockfile matching). Add `cargo-fuzz` via a separate pinned nightly Rust image
   and offline Python/Node fuzzing profiles.
5. **Storage quotas & artifact retention controls:**
   Enforce per-volume and aggregate disk quotas/cleanup policies for Docker
   volumes and `artifacts/` directories.
6. **Optional campaign extensions:**
   Add automatic reproducer input minimization, deterministic scanner-generated
   investigation targets, semantic finding deduplication (beyond exact
   location/root-cause matching), and optional candidate carryover across
   attempts.
7. **Controller-mediated external capabilities (workers stay `--network none`):**
   - Optional opt-in controller-side `web_search` tool (e.g. via a configured
     search instance) with query/turn caps, full event logging, stall-guard
     accounting, and untrusted-output framing; any future page fetcher would
     require redirect-aware SSRF blocking (loopback/private ranges) and strict
     type/size limits.
   - Controller-side MCP client interface (logging and bounding tool calls on
     the host rather than delegating to the inference server).
   - Controller-mediated allowlisted package mirrors/proxies for controlled
     offline dependency testing.
