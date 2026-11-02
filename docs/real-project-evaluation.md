# Real-project evaluation of the condensed history record

Status: proposal. Nothing here has been run. The only live evidence so far is two
runs on a two-file fixture (see AGENTS.md, 2026-11-02), which showed the record
reaching the model but no measurable benefit.

## Question

On a real project, over runs long enough that history is trimmed, does the
condensed record (`MAX_SUMMARY_CHARS=3000`) reduce repeated commands and improve
outcomes compared with no record (`MAX_SUMMARY_CHARS=0`)?

Hypothesis: with the record, the share of repeated commands drops and more runs
reach an accepted completion or a useful finding. Expect a small effect at best,
and plan on a null result being a valid outcome.

## 1. Choose the project

Pick one project that satisfies all of these:

- Builds and runs its tests offline after a networked preparation step. Python 3.13
  or Go is the easiest start (no compiler flags, simple lockfiles).
- Roughly 5k-30k lines, so an investigation needs more than a handful of file reads.
- Has a **known, already-fixed bug**: check out the commit just before the fix. This
  is the ground truth. Prefer a bug found by reading code, not one that needs a
  special input corpus.
- Tests pass at that commit, so the protected baseline can pass.

Use a disposable copy. Do not commit third-party source to this repository. Treat
the project as untrusted, as with all targets.

## 2. Prepare it

1. Write a manifest next to the examples (`examples/python313.json` is the model):
   `name`, `source` (path to the pinned checkout), `profile`, `prepare` commands
   (dependencies pinned and installed here, the only networked step), `check`
   (offline test command) and a `verification` block.
2. Copy `examples/verification/<language>/check.py` as the protected verifier and
   adapt it to run the project's tests. Keep it outside the project source.
3. Build and prepare, then confirm the offline baseline:

   ```sh
   python3 sandbox.py build python313
   python3 sandbox.py prepare <manifest>.json --state artifacts/<name>.json
   python3 sandbox.py check artifacts/<name>.json
   ```

   Do not continue unless the baseline passes with networking disabled.
4. Check free disk first (`df -h .`). Each run retains artifacts; the host has been
   tight before (about 12 GB free).

## 3. Fix the experimental setup

Keep everything identical between conditions except one variable.

| Setting | Value |
| --- | --- |
| Model | `google/gemma-4-12b` (optionally repeat once with another model later) |
| LM Studio | Loaded context length noted in the results; nothing else using the GPU |
| `MAX_HISTORY_MESSAGES` | `12` (so trimming happens within a few steps) |
| `MAX_CONTEXT_TOKENS` | Below the loaded context length, with margin |
| `--steps` | `40` |
| `--timeout` | `1800` |
| Task text | Same for both conditions, see below |
| **Variable** | A: `MAX_SUMMARY_CHARS=0` (control). B: `MAX_SUMMARY_CHARS=3000` (default) |

Task (does not name the bug, only the area):

> Investigate `<module or function scope>` for correctness and security bugs.
> Form a hypothesis, check it with commands, and retain a minimal reproducer or
> precise evidence in evidence.txt. Do not repeat a command whose result you
> already have. Finish with finish_task when you have a supported conclusion.

Decide this wording before the first run and do not change it. If you want to test
the "do not repeat" sentence as well, treat that as a separate third condition (C)
rather than mixing it into A and B.

## 4. Run

Use `sandbox.py run` (not a campaign) so runs are independent: campaigns deduplicate
identical tasks and cap attempts per target. Five runs per condition, interleaved
(A B A B ...) so drift in the machine or model server affects both equally:

```sh
for i in 1 2 3 4 5; do
  for chars in 0 3000; do
    MAX_SUMMARY_CHARS=$chars MAX_HISTORY_MESSAGES=12 \
      .venv/bin/python sandbox.py run artifacts/<name>.json "<task>" \
      --model google/gemma-4-12b --steps 40 --timeout 1800 \
      | tee -a docs/eval-runs.log
  done
done
```

Each run prints `Run <status>; evidence: <path>`. The shell loop needs the host
venv (`.venv/bin/python`). Run it in the background and expect an hour or more.
Do not run other LM Studio work at the same time. Keep `docs/eval-runs.log` as the
mapping from run directory to condition (add `echo "chars=$chars"` before each run
if easier to read).

## 5. Measure

For every run directory under `artifacts/runs/`, take from `result.json` the
`status` and `error`, and from `events.jsonl` the commands:

```sh
python3 - <<'PY'
import json, sys
for run in sys.argv[1:]:
    r = json.load(open(f"{run}/result.json"))
    cmds = [json.loads(l)["command"] for l in open(f"{run}/events.jsonl")
            if '"kind": "command"' in l and not json.loads(l).get("readonly")]
    print(run.split("/")[-1], r["status"], r.get("error", ""),
          "commands", len(cmds), "distinct", len(set(cmds)),
          "repeat_ratio", round(1 - len(set(cmds)) / max(1, len(cmds)), 2),
          "verified", r.get("candidate_verified"))
PY
```

(Pass the run directories as arguments. Protected-check commands are flagged
`readonly` and excluded.)

Record per run:

- Final status and stop reason (completed / exhausted / stalled / failed).
- Commands, distinct commands, repeat ratio.
- Whether it stalled on the repetition guard.
- Whether `evidence.txt` exists and **you** judge it correct, partially correct,
  wrong, or unrelated to the known bug. Judge blind to condition if you can, by
  having the runs relabelled before you read them.
- Wall-clock time.

Optionally capture the prompt the model sees, as in the earlier live check, to
confirm the record was populated in condition B (it should list entries after about
step 6 and be absent in A).

## 6. Decide

With five runs per condition only large effects are visible. Suggested reading:

- **Keep as is**: B has a clearly lower repeat ratio (say at least a quarter lower
  on average) and no fewer correct or completed runs than A.
- **Try the prompt tweak** (condition C): B is not better, but repeats in B are of
  commands that appear in the record. Add the "do not repeat" instruction to the
  record header and rerun.
- **Revert or leave disabled by default**: B is no better than A, or worse (for
  example the record fills the budget and shortens the window with no benefit).
- **Inconclusive**: results differ run to run more than between conditions. Add
  runs or use a second target scope before concluding anything.

Passing verification or a finished run is not a security verdict, and a finding
that matches the known bug is evidence about the model, not about the project.

## 7. What to send back

- Project, commit, language, manifest, and the known bug used as ground truth.
- LM Studio model, loaded context length, and any non-default settings.
- A table with one row per run: condition, directory, status, commands, distinct,
  repeat ratio, evidence quality, minutes.
- Anything odd: timeouts, LM Studio errors, disk problems.
- The run directories for any run you want me to read in detail.
