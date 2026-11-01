# Bounded campaigns

Create a campaign from a prepared state and an operator-authored JSON array of task strings:

```sh
python3 sandbox.py campaign create artifacts/my-campaign prepared.json tasks.json
python3 sandbox.py campaign resume artifacts/my-campaign --max-runs 3 --steps 20 --timeout 1800 --model google/gemma-4-12b
```

Each attempt starts from the prepared project and retains the normal run artifacts under the campaign directory. Identical task strings are queued once. A completed task is skipped on resume; failed, exhausted, and interrupted tasks are retried on the next invocation, up to `--max-attempts` (default 3 attempts per task across invocations). Raise that explicit limit to authorize more attempts. A run failure stops the current invocation. The run limit bounds attempts per invocation, while steps and timeout bound each attempt. The campaign records its stop reason and uses a lock to prevent concurrent resumes. An attempt left running by a terminated controller is marked interrupted when resumed. Review individual result files and evidence; completion does not establish security.

Record an operator-reviewed observation using `python3 sandbox.py campaign finding artifacts/my-campaign finding.json`. For example:

```json
{"title":"Potential overflow","location":"src/parser.c:42","root_cause":"unchecked length","status":"suspected","evidence":["runs/0/ATTEMPT/RUN/events.jsonl"]}
```

Evidence paths must refer to existing files under the campaign's runs directory. The operator supplies the status: `confirmed`, `suspected`, `rejected`, `blocked`, or `no_finding`. Observations with exactly matching location and root cause are grouped in `findings.json`, retaining each observation and its evidence. This is a review aid, not proof that reports share a root cause or that a finding is valid. Run artifacts can contain untrusted output; inspect and replay evidence independently.

Replay an operator-owned command file against a recorded run:

```sh
python3 sandbox.py campaign replay artifacts/my-campaign runs/0/ATTEMPT/RUN reproducer.sh --timeout 600
```

Both snapshots must exist. The command is run in fresh, separate offline workers with the normal resource and command output limits. Build steps and reproducer inputs must be available in each snapshot or the prepared image. Each side receives its own timeout. Results are retained under `runs/replays/`, including the exact command, snapshot hashes, exit codes and bounded diagnostics, and can be linked as finding evidence. `failure_then_pass` records a nonzero original exit other than timeout followed by a zero candidate exit. Inspect diagnostics to distinguish the expected bug from unrelated build, dependency, or command failures before recording a confirmed finding. Replay does not replace protected verification or minimize test cases automatically.

Campaigns do not automatically extract findings, carry candidates between runs, track coverage, detect repeated commands, or select investigation targets. Task and finding identity comparisons use exact strings. Protect the campaign directory as operator-owned data.
