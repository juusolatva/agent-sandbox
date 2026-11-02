# Bounded investigation campaigns

Stage 4 adds persistent target selection, progress and stall tracking, structured
finding proposals, operator review, and fresh-worker replay. Campaigns use the
Stage 3 controller and prepared projects; project commands remain offline.

Create a campaign from an existing prepared state and an operator-authored JSON
array of task strings or target objects. For example, `targets.json`:

```json
[
  {"task":"Investigate length handling", "scope":"src/parser.c:decode", "priority":10},
  {"task":"Investigate error cleanup", "scope":"src/io.c", "priority":0}
]
```

```sh
python3 sandbox.py campaign create artifacts/my-campaign prepared.json targets.json
python3 sandbox.py campaign resume artifacts/my-campaign --max-runs 3 --steps 20 --timeout 1800 --budget 3600 --continue-on-failure --model google/gemma-4-12b
python3 sandbox.py campaign status artifacts/my-campaign
```

The scheduler selects less-attempted targets first, breaking ties by descending
priority and input order. Identical task/scope pairs are queued once. Each
invocation attempts each eligible target at most once, up to `--max-runs`.
Completed tasks are skipped. Failure stops the invocation unless
`--continue-on-failure` explicitly enables continued selection. The next resume
prioritizes previously unattempted targets over retries.

`--max-attempts` defaults to three attempts per target across invocations. Two
successive unsuccessful attempts with identical candidate source snapshots mark
a target stalled (`--stall-limit` adjusts this threshold). Report wording is
excluded from this comparison. Three identical command/result pairs within a run
stop it and mark its target stalled. Stalled targets are skipped; review their
evidence and create a new campaign with a revised task to investigate further.
Raising `--max-attempts` permits additional retries of targets that reached only
the attempt limit.

The budget bounds selection and the timeout passed to each controller run. When
omitted, it defaults to `max-runs * timeout`. Steps and command/output limits
remain enforced by the controller. Container startup, transfers, and cleanup have
separate bounded allowances and can extend elapsed time beyond the investigation
budget. Artifacts accumulate across invocations; monitor host storage as described
in STAGE3.md. There is no automatic artifact deletion or host disk quota.

Campaign state records attempts, assigned scopes, artifact references, timestamps,
proposal import status, and stop reasons. `attempted_at` records an attempt; it is
not evidence that every path in a scope was examined. `all_completed` means the
controller accepted each task's completion. `run_limit`, `time_budget`,
`attempt_failed`/`attempt_exhausted`/`attempt_interrupted`, `interrupted`, and
`no_eligible_targets` explain other stops. Only `all_completed` returns CLI exit
code zero. A clean completion or a no-finding claim is not a security verdict.

State writes are atomic and synced. An exclusive lock prevents concurrent campaign
mutations. An attempt left running by a terminated controller is marked interrupted
on resume; any retained artifacts remain available, and retry uses a fresh project
copy. New campaigns pin a canonical prepared-state hash and reject changed state.
Existing version 1 campaigns remain readable but lack this integrity check.

## Finding proposals and review

The task prompt asks the model to retain `investigation-findings.json`:

```json
{
  "summary":"Examined parser bounds; evidence and limitations described here",
  "disposition":"finding",
  "findings":[{
    "title":"Potential overflow", "location":"src/parser.c:decode",
    "root_cause":"unchecked length", "hypothesis":"length can wrap",
    "conditions":"large input", "reproducer":"description for operator review"
  }]
}
```

A report may instead claim `no_finding` or `blocked` with an empty findings list.
The controller requests hypotheses, necessary conditions, and evidence. After a
run, the campaign imports the report from the retained candidate snapshot into
`proposals.json`. Reports are bounded to 64 KiB and 100 findings. Missing and invalid
reports are recorded explicitly. Proposal IDs group exact location/root-cause
matches in `campaign status`; all per-run observations remain retained.

Model reports are untrusted. Imported findings always have `suspected` status and
controller-selected evidence references. Model-supplied statuses and host paths
cannot confirm findings or direct execution. Report claims do not change queue
acceptance, verification policy, or the reviewed findings ledger.

Inspect the referenced report and run artifacts, replay the failure, and record an
operator decision using zero-based target, attempt, and finding indices:

```sh
python3 sandbox.py campaign review artifacts/my-campaign 0 0 0 --status confirmed
```

Supported decisions are `confirmed`, `suspected`, `rejected`, `blocked`, and
`no_finding`. Review appends to `findings.json`, retaining earlier observations.
A repeated location/root cause groups observations without proving equivalence.
An operator can also import a manually written observation with
`campaign finding DIRECTORY finding.json`:

```json
{"title":"Potential overflow","location":"src/parser.c:decode","root_cause":"unchecked length","status":"suspected","evidence":["runs/0/ATTEMPT/RUN/events.jsonl"]}
```

Evidence paths must refer to existing files under the campaign's runs directory.
Keep the campaign directory operator-owned; it is never mounted into workers.

## Reproducer replay

Replay an operator-owned command file against a recorded run:

```sh
python3 sandbox.py campaign replay artifacts/my-campaign runs/0/ATTEMPT/RUN reproducer.sh --timeout 600
```

Both original and candidate snapshots must exist. The same command runs in fresh,
separate offline workers with the normal resource and output limits. Build steps
and reproducer inputs must be available in each snapshot or prepared image. Each
side receives its own timeout. Reports under `runs/replays/` retain command text,
image identity, snapshot hashes, exit codes, and bounded diagnostics. Link those
reports as finding evidence using `campaign finding`.

`failure_then_pass` records a nonzero original exit other than timeout followed by
a zero candidate exit. Inspect diagnostics to distinguish the expected defect
from unrelated build, dependency, or command failures. Replay does not replace
protected verification. Automatic input minimization, generated scanner leads,
and candidate carryover are optional future extensions; they are not performed
by this workflow.

## Verification

The integration tests use fake inference with real offline Docker workers. They
cover proposal import and review, expected original failure and candidate success,
replay timeout handling, and repeated-command termination with retained artifacts.
The full regression suite also covers all six language baselines, protected-check
tampering, detached-process cleanup, and sanitizer startup. These checks validate
the harness workflow; investigation quality with live models remains task-dependent.
