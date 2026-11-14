# Security Policy

## System and Scope

This repository runs local LLM investigations against disposable project copies.
Scope includes the host CLI/controller, action parsing, Docker execution and
transfer helpers, preparation, verification, campaigns, advisory scanning,
images, Compose configurations, and CI.

The primary workflow is `sandbox.py run` and its campaign commands. Legacy
Compose runs `agent.py` inside the command container and has different boundaries:
it permits access to the inference proxy and, by default, a host workspace mount.
Do not assume the primary workflow's isolation applies to Compose.

## Threat Model and Trust Boundaries

The local operator controls manifests, verification policy, task selection,
inference endpoints, prepared-state files, and artifact/campaign directories.
These inputs require operator ownership; project-supplied configuration is not
trusted merely because it has the expected filename or schema.

Treat project files, dependencies and build scripts, command output, model
responses, candidate snapshots, lockfiles, and finding reports as untrusted.
An attacker may control these inputs and induce arbitrary commands in a worker.
Prompt injection and malicious project code are expected boundary conditions.

Protect host files and credentials, the Docker daemon, verification policy,
prepared identity, other runs, and the integrity of retained outcomes.
The host controller and Docker runtime are trusted infrastructure; review how
repository code exposes their privileges to untrusted inputs.

Preparation and advisory downloads deliberately use networking. Investigation,
verification, replay, and lockfile extraction are intended to run offline.
The controller contacts the operator-selected inference service and sends it
task and project-derived content. Model responses remain untrusted.

## Security Invariants

- Model commands execute inside designated workers, never as host shell commands.
  Model content cannot select Docker mounts, privileges, or execution policy.
- Primary workers receive neither host credentials nor the Docker socket.
  Their network isolation, filesystem boundaries, and resource restrictions
  must hold when project code is malicious.
- Transfers must not escape their designated roots, follow unsafe links, import
  special files, or bypass enforced size and file-count limits.
- Candidate edits cannot replace protected checks or acceptance commands.
  Verification uses fresh state and prepared dependencies, with candidate source
  read-only. Completion requires explicit action and successful protected checks.
- Parsing, output retention, commands, inference, and investigation cycles remain
  bounded. Failures and exhausted budgets must not become successful completion.
  Cleanup must address worker descendants as well as their parent process.
- Model reports remain proposals. Their statuses, paths, and reproducer text
  cannot authorize host operations, execution, or reviewed finding decisions.
- Run artifacts and campaign state remain outside model-writable storage.
  Recorded outcomes distinguish verification, completion, and operator review.
- Offline advisory scans must not silently enable networking or report clean
  results when required inputs are absent, stale, corrupt, or scanning fails.

## Reportable Findings and Severity Context

Report concrete paths from attacker-controlled input to a violated boundary or
security property. Examples include host access, credential disclosure,
cross-run interference, verification-policy tampering, unsafe extraction,
false successful outcomes, and bypasses of enforced availability limits.

Arbitrary shell execution inside a designated worker is intended functionality.
Operator-selected host paths are not vulnerabilities solely because they are
configurable. In either case, unintended authority or boundary crossing remains
reportable. Assess preparation, primary workers, Compose, and CI separately.

Severity depends on reachable impact and prerequisites. Container placement,
offline operation, or passing tests alone do not establish low severity.

## Known Limitations and Risk Decisions

Containers share the host kernel. Per-volume and aggregate artifact disk quotas
are absent. Protected tests cannot prove correctness or defeat every deliberately
test-aware program. Legacy Compose shares command and inference trust boundaries;
its prepared override also permits a writable container root filesystem. For
that reason, Compose passes only a fixed dummy API key into the sandbox and does
not support inference servers that require credentials.

The controller supports plaintext HTTP when pointed at a local or operator-specified
endpoint. Older version 1 campaigns lack prepared-state integrity checks. These limitations
are not blanket exclusions. The recorded decision to monitor unresolved base-image CVEs
does not suppress new evidence of exploitability or impact.
