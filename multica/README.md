# Multica Integration

This directory defines the desired Multica project, squad, roles, and daily runbook. The contract
alone is not proof that the external workspace is connected; the adapter status, daemon status,
and git-ignored bootstrap state are the runtime evidence.

## Local status and issue preparation

```bash
.venv/bin/python -m trading_team.multica_adapter status
.venv/bin/python -m trading_team.multica_adapter prepare --date YYYY-MM-DD
```

`prepare` writes the issue description under the git-ignored `trading_team/.runs/multica/`
directory. Once the Multica CLI is installed, authenticated, and its daemon has a runtime, use:

```bash
.venv/bin/python -m trading_team.multica_adapter dispatch --date YYYY-MM-DD
```

Bootstrap the declared Project, local-directory resource, seven Agents, Squad, weekday Autopilot,
and schedule by passing real IDs from `multica runtime list --output json` and daemon status:

```bash
.venv/bin/python -m trading_team.multica_adapter bootstrap \
  --runtime-id <runtime-id> \
  --daemon-id <daemon-id>
```

The command reads existing resources first and only creates missing exact-name/path matches. It
writes non-secret IDs to the git-ignored `trading_team/.runs/multica/bootstrap.json`; the console
uses this file to distinguish a repository contract from resources that actually exist in Multica.
It also updates the existing Autopilot description and issue-title template when the repository
runbook changes. `dispatch` remains fail-closed until the CLI, login, daemon, bootstrap, and
assignee are real.

As of 2026-09-22, CLI `0.5.1`, Cloud authentication, the local daemon, the Codex runtime, and all
declared resources are live. A repeated real bootstrap returned no actions. The Autopilot is bound
to the Research Lead agent because Multica requires an agent assignee; manually prepared issues
remain bound to the Squad by exact `--assignee-id` so the leader can route the staged runbook. Its
issue title is `A-share daily research {{date}}`; the runbook requires that date to be passed
explicitly to `kimi_daily`. The September 22 natural Run failed with a newer-Codex requirement;
it is not an accepted research run. Current read-only observation also reports Runbook drift.

Only Research Lead can invoke the complete publisher. Its seven local LLM steps are not seven
independent Multica executions; bounded review delegation and the full remote maker/checker chain
still require real acceptance. No runtime, schedule or remote Runbook was changed by this local edit.
See [the acceptance matrix](/Users/henry/projects/quant/docs/SYSTEM_ACCEPTANCE.md) before calling
the integration complete or running a bootstrap/dispatch operation.

## Versioned review handoffs

The local six-role review contract now seals bounded inputs, verifies the submitting task/role,
records structured evidence in the Run ledger, and invalidates downstream reviews when their
source or upstream receipt changes. Whole-workflow backup/restore retains this chain.

```bash
.venv/bin/python -m trading_team.multica_reviews status --date YYYY-MM-DD
.venv/bin/python -m trading_team.multica_reviews prepare --date YYYY-MM-DD --role data-quality --team <isolated-root>/trading_team
```

Status is read-only; preparation writes a local package but does not create an Issue or dispatch.
Use the returned full versioned title and assignee only after authorized task creation. Submission
requires real running-task verification. Read [the review contract](/Users/henry/projects/quant/docs/MULTICA_REVIEWS.md)
for schemas, retries and recovery. A succeeded recording can contain a rejected review.
This post-publication review evidence now gates new positive approvals and both public LLM Paper
entrypoints. Dual-Paper receipts bind the review version; finalization retries cannot silently pair
new reviews with old settlement. The console exposes dated review states and recording versus
acceptance. This is not a pre-publication gate or an accepted remote maker pipeline. All six-role
execution tests so far use isolated fixtures; real remote acceptance remains incomplete.
