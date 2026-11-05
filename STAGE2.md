# Language environments and offline preparation

Stage 2 adds operator commands in `sandbox.py`. Python's standard library and
Docker with BuildKit/Compose 2.24.4+ are sufficient on the host. Commands execute
project build hooks inside containers; manifests are trusted operator input.

## Try a bundled fixture

```bash
python3 sandbox.py build cpp
python3 sandbox.py prepare examples/cpp.json --state artifacts/cpp.json
python3 sandbox.py check artifacts/cpp.json
```

Repeat with `go`, `rust`, `python313`, `python314`, or `node` and the corresponding
example JSON. Each `build` probes the toolchain with networking disabled. Each
`prepare` snapshots the selected source, installs dependencies while networked,
then runs the manifest's check in a fresh container with `--network none`.
It saves state only when that check passes. `check` replays that prepared snapshot,
not changes subsequently made in your source checkout. Re-prepare after changes.

## Configure a real project

Copy an example manifest and set `source` relative to that manifest's directory:

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
  "memory": "4g",
  "cpus": 2,
  "pids": 256,
  "timeout": 600
}
```

`prepare` entries run in separate Bash processes with `-e`. Files persist between
steps, shell variables and activated environments do not. Use absolute or
workspace-relative executable paths. Project build dependencies must be declared
in the lockfile too. Native dependencies not present in an image need a custom
Dockerfile target; do not install them during offline checks.

Typical dependency preparation:

- Go: `go mod download`; offline check `GOPROXY=off go test -mod=readonly ./...`.
  GOTOOLCHAIN is local; choose a matching toolchain rather than auto-downloading.
- Rust: commit Cargo.lock, then `cargo fetch --locked`; check `cargo test --frozen`.
  Build scripts that download external files need explicit preparation as well.
- Node: commit package-lock.json and use `npm ci` during preparation. Test a
  fresh `npm ci --offline` if dependency reconstruction is required. Install
  scripts may need separately cached native binaries. The bundled fixture has
  no dependencies and creates its lockfile just to demonstrate the workflow.
- C/C++: prepare fetched third-party inputs, submodules, and CMake downloads;
  offline builds must use those local inputs.

All prepared images include project source and dependencies. `.git`, `.env*`,
credential directories/files, and common build caches are excluded from source
snapshots. External symlinks are rejected. These defaults do not detect secrets
embedded in arbitrary source files: select and inspect the input directory.
The source directory is never mounted into preparation or execution containers.
Prepared images are local artifacts; do not publish them without reviewing their
contents. The state JSON records source hash, operator configuration, base image
ID, and prepared image ID. Checks use the recorded image ID rather than a tag.

## Run the agent on a prepared project

Stage 3 now provides the prepared-project runner. See [STAGE3.md](STAGE3.md)
for host SDK setup, protected verification configuration, and retained artifacts.
For example, after preparing the bundled Python project:

```bash
.venv/bin/python sandbox.py run artifacts/python313.json \
  "Investigate a bounded area and retain evidence." --model google/gemma-4-12b
```

Stage 2's prepared Compose override remains available for legacy use; the Stage 3
runner uses offline per-command containers and does not start that proxy.
Manifest resource limits apply to Stage 3 workers as well as baseline checks.

## Versioning and proxy trust

`docker/images.json` lists language targets. Dockerfile defaults select Python
3.13.18 / 3.14.10, Go 1.27.3, Rust 1.99.1, and Node 24.22.1. C/C++ uses Debian
Trixie GCC 12 and Clang 19, including sanitizer runtimes. Builds resolve Debian
security package updates; rebuilding is not bit-for-bit reproducible. Prepared
image IDs preserve the actual environment used. Go/Rust/Node sources are copied
from versioned official images, rather than downloaded by shell installers.

Image overrides are available through Docker build arguments. Projects requiring
other versions should use a matching custom image; change the profile definition
and rebuild before preparing that project. Optional tools such as Staticcheck,
govulncheck, cargo-audit, Ruff, and pytest must currently be installed with pinned
versions during preparation or in a custom target. Advisory checks require a
supported local database or an imported, dated result from preparation.

Builds preserve Docker's proxy settings and TLS verification. Where a managed
proxy needs additional CA trust, pass a combined PEM trust bundle:

```bash
python3 sandbox.py build python314 --ca /path/to/combined-ca.pem
python3 sandbox.py prepare projects/my-project.json \
  --state artifacts/my-project.json --ca /path/to/combined-ca.pem
```

The bundle is a BuildKit secret mount, not copied into an image. pip and common
project package clients receive trust settings only during networked steps.
BuildKit dependency installation and project execution remain separate from the
host environment. Registry pulls use the daemon's existing trust configuration.

## Local validation

All six images were built, probed, and used to prepare and test their bundled
fixtures with networking disabled on 2026-11-01. Python fixtures installed pytest
8.4.2 during preparation and passed offline under both interpreters. GCC/Clang
checks included AddressSanitizer/UndefinedBehaviorSanitizer; Rust tests included
Clippy. Hosted CI and live LM Studio integration have not been exercised.

## Analysis tools in the images

Installed at image build time (pinned, no network needed to run) and probed by
`sandbox.py build`:

| Profile | Tools |
| --- | --- |
| `cpp` | clang-tidy 19, ASan/UBSan, libFuzzer (`clang++-19 -fsanitize=fuzzer,address`) |
| `go` | Staticcheck 2026.2.2 (v0.8.2), `go vet`, native fuzzing (`go test -fuzz`) |
| `rust` | Clippy, rustfmt |
| `python313`, `python314` | Ruff 0.17.1, Hypothesis 6.170.1, pytest per project |
| `node` | ESLint 10.13.0 (flat config: put `eslint.config.js` in the project) |

Offline checks: libFuzzer found a planted crash in the cpp image and `go test -fuzz`
found one in the Go image with `--network none`. Not installed: govulncheck and
cargo-audit (their vulnerability databases are not the OSV archives above, so they
cannot run offline yet; use `advisories scan`), cargo-fuzz (needs nightly Rust),
and Python/Node fuzzers. These tools run only when a manifest `check` or the model
invokes them; none changes the default baselines.

## Offline dependency advisory checks

Advisory data is prepared while networked and consumed offline. Both commands run
on the host, outside any model-controlled container. They need
[OSV-Scanner](https://google.github.io/osv-scanner/) 2.x on `PATH` for scanning.

```bash
python3 sandbox.py advisories fetch artifacts/advisories            # all ecosystems, ~270 MB
python3 sandbox.py advisories fetch artifacts/advisories --ecosystem Go --ecosystem crates.io
python3 sandbox.py advisories scan artifacts/go.json artifacts/advisories \
  --output artifacts/go-advisories.json
```

`fetch` downloads OSV ecosystem databases (Go, PyPI, crates.io, npm) from
`osv-vulnerabilities.storage.googleapis.com`, checks each archive, and writes
`metadata.json` (SHA-256, size, upstream `Last-Modified`, fetch time) last, so a
failed download never replaces a good snapshot. Repeated runs refresh the listed
ecosystems and keep the others.

`scan` copies recognised lockfiles (`go.mod`, `Cargo.lock`, `package-lock.json`,
`requirements*.txt`, `poetry.lock`, `uv.lock`, `Pipfile.lock`) out of the prepared
image with networking disabled, re-verifies the snapshot hashes, and runs
OSV-Scanner with `--offline --offline-vulnerabilities`. Exit status: `0` clean,
`1` known advisories found, `2` the check is incomplete (missing ecosystem
database, no lockfile, scanner unavailable or failing, tampered database, or a
snapshot older than `--max-age-days`, default 7). Incomplete is never reported as
clean. The JSON report records the snapshot's age and hashes.

Limits: this matches declared dependency versions to known advisories only. It
does not show code is safe, does not analyse reachability (govulncheck) or Cargo
audit semantics, skips vendored/`node_modules`/`.venv` copies, and sees only
lockfiles that exist in the prepared source; the bundled Python fixture installs
pytest at preparation time, so it has nothing to check. Snapshots are not baked
into prepared images, and the model cannot reach them.
