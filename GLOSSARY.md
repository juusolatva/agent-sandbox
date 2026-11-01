# Offline Project Investigation

The language of preparing projects for offline investigation, carrying out agent
tasks, and assessing the resulting changes and evidence.

## Language

### Project preparation

**Target project**:
The project whose behavior and source are the subject of an investigation.
_Avoid_: Sandbox, harness

**Language profile**:
A supported choice of language toolchain for preparing and investigating a target
project.
_Avoid_: Project, prepared project

**Project manifest**:
The operator's declaration of a target project's source, language profile,
preparation requirements, checks, and execution limits.
_Avoid_: Prepared state, task

**Preparation**:
The setup and baseline assessment of a target project and its dependencies before
offline investigation.
_Avoid_: Investigation, verification

**Prepared project**:
A captured target project with its toolchain and dependencies ready for offline
checks and investigation.
_Avoid_: Source checkout, workspace

**Prepared state**:
The record identifying a prepared project, its source and verification identities,
and the operator configuration used to prepare it.
_Avoid_: Run state, campaign state

### Investigation

**Task**:
The requested goal of an agent's work on a target project.
_Avoid_: Run, step

**Run**:
One attempt by an agent to carry out a task, with its own workspace, evidence, and
outcome.
_Avoid_: Task, campaign

**Step**:
One agent response within a run, which may contain command actions, a completion
request, or no valid action.
_Avoid_: Command, run

**Investigation workspace**:
The editable project copy belonging to a run, including changes and outputs
produced during that run.
_Avoid_: Source checkout, prepared project, candidate

**Completion request**:
The agent's explicit claim that its task is finished, submitted for acceptance.
_Avoid_: Completion, verification result

**Run outcome**:
The recorded reason a run ended: completed after accepted completion, failed due
to a run-level failure, exhausted after using its step allowance, or interrupted
by an external interruption.
_Avoid_: Verification result, security verdict

### Checks and acceptance

**Workspace check**:
A project check whose expectations may come from files the agent can edit in its
investigation workspace.
_Avoid_: Protected check, independent verification

**Protected check**:
An operator-owned acceptance check whose expectations the agent cannot change.
_Avoid_: Workspace check, model self-assessment

**Baseline**:
A check result for the prepared project before the agent makes changes, providing
a reference for assessing later candidates.
_Avoid_: Candidate verification, security certification

**Candidate**:
The captured set of eligible project files from an investigation workspace,
available for assessment and review regardless of the run outcome.
_Avoid_: Verified fix, accepted patch, complete workspace

**Protected verification**:
An assessment of the prepared project or a candidate against protected checks,
independent of the agent's mutable investigation environment.
_Avoid_: Workspace check, proof of security

**Accepted completion**:
A completion request accepted after the required verification passes; it records
task acceptance without establishing that the target project is free of defects.
_Avoid_: Model self-assessment, security certification

### Retained results

**Source snapshot**:
A captured set of project files at a particular point in preparation or
investigation.
_Avoid_: Full environment backup, source revision

**Original snapshot**:
The source snapshot at the start of a run, after project preparation and before
agent changes.
_Avoid_: Original checkout, candidate

**Run artifact**:
A retained record or output of a run, such as its outcome, execution history,
source snapshots, or proposed patch.
_Avoid_: Finding, verified result

**Evidence**:
Retained observations and supporting material for assessing an investigation's
claims; evidence may include untrusted agent or project output.
_Avoid_: Proof, confirmed finding

**Proposed patch**:
The differences between a run's original snapshot and its candidate, available for
review independently of whether the run completed or verification passed.
_Avoid_: Applied fix, accepted patch

### Campaigns and finding review

**Campaign**:
A durable queue of investigation tasks with retained attempts and bounded execution.

**Attempt**:
A campaign's record of invoking a run for one task, including its outcome and artifact references.

**Finding observation**:
An operator-recorded assessment with a status and references to evidence. Observations
with the same location and root-cause text are grouped for review.

**Reproducer replay**:
Execution of an operator-selected command against original and candidate snapshots in
separate workers, retaining both results for assessment.
