# ISM Code Health — Stateful Nightly Ops Sub-Project

Self-contained tracking, discovery, and Swarm orchestration directory for recurring autonomous code-health runs across `ism-frappe` and `ism-agent-os`.

Each run builds directly on prior runs: a zero-token static scanner + a read-only LLM semantic auditor continuously discover new issues and rotate through all 11 disjoint codebase zones, while parallel isolated fixers land verified, bounded improvements onto your active nightly branch without ever touching `main` or `development.localhost:8000`.

---

## Directory Layout

| Path | Owner | Purpose |
| :--- | :---: | :--- |
| `README.md` | Human / Coordinator | Quick-start, launch prompts, and morning review flow |
| `POLICY.md` | Human | Quota presets, blast-radius caps, forbidden files, and triage rules |
| `SWARM_PLAYBOOK.md` | Coordinator | Step-by-step `/swarm` execution protocol, Gemini 4 model pinning, and brief templates |
| `zones.json` | Coordinator | Canonical 11-zone disjoint map of `ism-frappe` and `ism-agent-os` with audit rotation state |
| `backlog.json` | Coordinator | Master issue ledger (`open` / `in_progress` / `awaiting_review` / `fixed` / `wontfix` / `deferred`) with fingerprint deduplication |
| `metrics_history.jsonl` | Coordinator | Append-only per-run metrics snapshots (LOC hotspots, static signals, open backlog trend) |
| `runs/` | Coordinator | Immutable morning-review dossiers (`YYYY-MM-DD_run-NNN.md`) + `_TEMPLATE.md` |
| `scripts/health_cli.py` | Shared (CLI) | Zero-dependency Python scanner, disjoint batch planner, and state mutator |
| `scripts/test_health_cli.py` | Shared (Tests) | Unit test suite for scanner rules, zone disjointness, deduplication, and batching |

> **Single-Writer Rule:** Only the Swarm `coordinator` modifies files inside `code-health/` (`zones.json`, `backlog.json`, `metrics_history.jsonl`, `runs/*.md`). Parallel `workspace: "share"` workers never edit `code-health/` directly, guaranteeing zero merge conflicts on state files.

---

## Nightly Launch Workflow (2 Steps)

### 1. Create a fresh nightly branch from `main`
```powershell
git checkout main
git checkout -b nightly/$(Get-Date -Format 'yyyy-MM-dd')
```
*(The Swarm coordinator refuses to run code edits if you are still on `main`.)*

### 2. Launch a Swarm session (`/teamwork` or `/swarm`) with one of the copy-paste prompts below

#### Standard Night *(Default — 1 Auditor + 2 Disjoint Fixers + 1 Cross-Family Verifier)*
```text
/swarm /model-triage
Run a Standard nightly code-health session following code-health/SWARM_PLAYBOOK.md and code-health/POLICY.md (preset: standard).
1. Verify we are on a non-main branch via `python code-health/scripts/health_cli.py preflight`.
2. Run `scan --seed --record-metrics` and `next-batch --preset standard --json` to pick 1–2 due audit zones and 2 strictly disjoint fix tracks.
3. Spawn monitor, auditor (read-only), fixer-a (workspace: share), fixer-b (workspace: share), and cross-family verifier per SWARM_PLAYBOOK.md.
4. Merge only verifier-approved commits onto this active nightly branch before killing workers, ingest new auditor findings into backlog.json, update zones.json, and write today's morning dossier under code-health/runs/.
```

#### Eco Night *(Lowest Quota — 1 Auditor + 1 Fixer + 1 Verifier)*
```text
/swarm /model-triage
Run an Eco nightly code-health session following code-health/SWARM_PLAYBOOK.md and code-health/POLICY.md (preset: eco).
Keep total code changes <= 250 LOC on this active nightly branch, verify with cross-family verifier, and write today's morning dossier under code-health/runs/.
```

#### Deep Audit Night *(Read-Only — Zero Code Edits, Zero Bench Slots)*
```text
/swarm /model-triage
Run a Read-Only Deep Audit code-health session following code-health/SWARM_PLAYBOOK.md and code-health/POLICY.md (preset: audit-only).
Audit the top 2–3 due zones in parallel using ast-outline and frappe-code-review, populate backlog.json with verified findings, update zones.json, and write today's dossier under code-health/runs/. Do not edit application code.
```

---

## Morning Review Workflow (5 Minutes)

1. Open the newest dossier in `code-health/runs/YYYY-MM-DD_run-NNN.md`.
2. Inspect the changes on your nightly branch:
   ```powershell
   git log --oneline main..HEAD
   git diff --stat main..HEAD
   ```
3. Optional triage: mark any unwanted backlog discovery as `wontfix` so the scanner never re-files it:
   ```powershell
   python code-health/scripts/health_cli.py update-issue --id CH-005 --status wontfix --note "Intentional guest endpoint"
   ```
4. If everything looks good, merge your nightly branch into `main` and promote:
   ```powershell
   git checkout main
   git merge --no-ff nightly/YYYY-MM-DD
   powershell -ExecutionPolicy Bypass -File scripts\promote-to-main.ps1 -Fast
   ```
   *(If you only want a subset of commits, `git cherry-pick` the verified fix commits you want plus the `code-health/` ledger commit.)*

---

## `health_cli.py` Cheat Sheet

```powershell
# Verify non-main branch & repo sanity
python code-health/scripts/health_cli.py preflight

# Inspect current backlog, due zones, and recent metrics
python code-health/scripts/health_cli.py status

# Run zero-token static & git-churn scan (read-only)
python code-health/scripts/health_cli.py scan --json

# Run scan and upsert deterministic findings + record metrics snapshot
python code-health/scripts/health_cli.py scan --seed --record-metrics

# Compute disjoint work packets for Swarm
python code-health/scripts/health_cli.py next-batch --preset standard --json

# Initialize today's run dossier from template
python code-health/scripts/health_cli.py init-run --preset standard

# Add / update backlog items & mark zones audited
python code-health/scripts/health_cli.py add-issue --from-json path/to/issues.json
python code-health/scripts/health_cli.py update-issue --id CH-001 --status fixed --resolved-run run-001 --commit <sha>
python code-health/scripts/health_cli.py mark-audited --zone frappe-scheduling --run run-001
```
