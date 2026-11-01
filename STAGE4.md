# Bounded campaigns

Create a campaign from a prepared state and an operator-authored JSON array of task strings:

```sh
python3 sandbox.py campaign create artifacts/my-campaign prepared.json tasks.json
python3 sandbox.py campaign resume artifacts/my-campaign --max-runs 3 --steps 20 --timeout 1800 --model google/gemma-4-12b
```

Each attempt starts from the prepared project and retains the normal run artifacts under the campaign directory. Identical task strings are queued once. A completed task is skipped on resume; failed, exhausted, and interrupted tasks are retried on the next invocation. A run failure stops the current invocation. The run limit bounds attempts per invocation, while steps and timeout bound each attempt. The campaign records its stop reason and uses a lock to prevent concurrent resumes. An attempt left running by a terminated controller is marked interrupted when resumed. Review individual result files and evidence; completion does not establish security.

Campaigns do not yet extract or deduplicate findings from model output, carry candidates between runs, or schedule tasks automatically. Task deduplication is exact string matching, not finding deduplication. Protect the campaign directory as operator-owned data.
