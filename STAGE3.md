# Isolated execution, protected checks, and retained evidence

Stage 3 changes `sandbox.py run` to a trusted host controller. It makes inference
requests itself and dispatches validated actions through fixed Docker operations.
Model-generated shell commands run only in worker containers with no networking,
no host credentials or Docker socket, a read-only root filesystem, and CPU,
memory, PID, and command limits. The host needs the pinned OpenAI SDK:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-controller.txt
```

## Prepare protected verification

Add an operator-owned verification directory to the manifest. It is relative to
the manifest, separate from the editable project snapshot:

```json
"verification": {
  "source": "verification/my-project",
  "command": "python3 -I /opt/sandbox/verification/check.py"
}
```

The preparation command bakes these files into `/opt/sandbox/verification` as
root-owned files. Worker root filesystems are read-only. The model cannot edit
these checks or the controller's acceptance command. Re-run preparation to
incorporate verification changes; the prepared state records a verification hash.
Preparation now requires both workspace and protected baselines to pass before
saving state. Stage 2 state without protected checks is refused by the Stage 3 runner.

Checks run from `/home/agent/workspace`, with prepared dependencies available.
Both the root filesystem and project home are read-only in verification workers.
Use `/tmp` for build products and caches. HOME, Go build cache, and Cargo target
directory point to temporary storage; GOPATH and CARGO_HOME retain prepared
module/crate locations. The examples demonstrate all six language profiles.
Checks that require writable source should be adapted to emit builds elsewhere.

Tests must exercise actual project behavior. Moving a command to a protected
file does not make mutable project tests independent: copy essential regression
checks into the operator-owned verification directory. Preserve original test
expectations and invoke trusted installed tools. Project code necessarily runs
during checks; a passing suite establishes only the tested behavior, not absence
of vulnerabilities or immunity to application code designed to recognize tests.

## Run a bounded investigation

```bash
python3 sandbox.py prepare examples/python313.json --state artifacts/python313.json
.venv/bin/python sandbox.py run artifacts/python313.json \
  "Investigate one area and retain a reproducer or precise evidence." \
  --model google/gemma-4-12b --steps 20 --timeout 1800
```

The controller tries `http://127.0.0.1:1234/v1`, then
`http://10.0.0.2:1234/v1`. Override with `--base-url` or `OPENAI_BASE_URL`.
Select the model with `--model` or `LLM_MODEL`; the controller requires an explicit
selection to avoid loading an arbitrary downloaded model. Use `--text-actions`
for models that need single XML actions without native tool calling. Malformed
actions remain rejected; tiny models may exhaust their budget without finishing. It never starts or
exposes the Compose TCP proxy. Keep LM Studio's existing authentication settings;
OPENAI_API_KEY is consumed only by the host client and never passed to a worker.

Every model command gets a fresh container. A named volume retains only this
run's project/home files between commands. Command containers are removed after
normal completion, timeout, and interruption, killing detached descendants too.
The volume is removed after the run. Processes and `/tmp` files do not persist
between commands; source files, prepared dependencies, and workspace outputs do.

Before inference, a fresh verifier checks the pristine prepared image. On each
completion attempt, the controller exports the candidate and creates another
fresh verifier from that same image, restoring only eligible source files. It
uses pristine dependencies and protected checks; the verification project mount
is read-only. A failed check rejects completion and returns bounded diagnostics
to the model. This remains a single bounded investigation; persistent campaigns,
queues, finding deduplication, and cross-run resume are Stage 4.

## Evidence and patch review

Each run creates `artifacts/runs/<unique-id>/` (override with `--artifacts`):

- `input.json` and `result.json`: prepared identity, task, configuration, model,
  endpoint, outcome, and separate baseline/candidate verification states.
- `events.jsonl`: validated execution results and assistant action messages.
- `controller.log`: bounded command/model-loop diagnostics.
- `original.json` and `candidate.json`: source snapshots with base64 file content.
- `candidate.patch`: Git-compatible text/binary/mode changes.
- `changes.json`: changed paths.

These files are controller-owned and not mounted into model workers. Candidate
files and messages remain untrusted evidence. Nothing is automatically applied
to your source checkout or published. To independently replay a saved candidate:

```bash
python3 sandbox.py verify artifacts/python313.json \
  --candidate artifacts/runs/RUN_ID/candidate.json
```

For patch review/application, use the original source revision matching the
recorded source hash. Review `changes.json`, then use `git apply --check` before
applying `candidate.patch`. Preparation may have added lockfiles/generated inputs,
so the original snapshot can differ from your original checkout. The binary
snapshot is retained alongside the patch to make that distinction reviewable.

Transfers accept regular files only, at most 5,000 files / 16 MiB total per
snapshot. Symlinks and special files fail closed. Dependency/cache directories,
credential patterns, `build`, `dist`, and `artifacts` are excluded from transfers;
changes there are neither proposed source changes nor accepted verifier inputs.
Put reproducers and logs you want retained under a regular directory such as
`evidence/` within these limits. A transfer failure records a failed run instead
of claiming completion; logs can survive even when no candidate can be exported.

The worker containers bound memory and processes, and the controller bounds
retained output, inference requests, step count, snapshot size, and run time.
Final artifact transfer/cleanup has a separate bounded allowance after a run
deadline. Docker volume disk usage still depends on available host storage;
per-volume storage quotas are not implemented. Container isolation shares the
host kernel and is not a replacement for a dedicated VM for hostile projects.

## Clang 14 sanitizer compatibility

On this WSL kernel an empty Clang 14 AddressSanitizer PIE executable crashed in
13 of 40 starts. LLVM describes the corresponding allocator-address conflict
with high-entropy ASLR in [D147984](https://reviews.llvm.org/D147984). The bundled
C++ fixture uses `-fno-pie -no-pie` only for its sanitizer executable, retaining
AddressSanitizer and UndefinedBehaviorSanitizer. A 100-start comparison passed
with that layout; the original configuration failed the same regression test.
Host ASLR and production project build flags are unchanged. For project builds
requiring PIE, select a newer Clang/compiler-rt with the allocator fix instead
of using this fixture-specific compatibility setting.

## Validation

All 33 local tests passed with Docker enabled, covering all six language
verifiers, rejection of rewritten workspace tests with broken application code,
detached-process cleanup, safe file transfer, patch application, and retained
evidence. The 100-start sanitizer regression passed after the scoped layout fix.
A live Gemma 4 12B run at 127.0.0.1:1234 wrote an evidence file, passed protected
verification, and completed through native tool calls. Tiny-model attempts that
failed to emit valid completion actions were retained as exhausted runs. Hosted
CI is configured but has not been run from this session.
