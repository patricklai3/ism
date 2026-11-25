# ISM Code Health — Stateful Nightly Ops Sub-Project

Self-contained tracking, discovery, and Swarm orchestration directory for recurring autonomous code-health runs across `ism-frappe` and `ism-agent-os`.

Each run builds directly on prior runs: a zero-token static scanner + rotating read-only LLM semantic auditors continuously discover new issues across all 11 disjoint codebase zones, while parallel isolated fixers land verified, bounded improvements onto your active nightly branch without ever touching `main` or `development.localhost:8000`.

---

## Directory Layout

| Path | Owner | Purpose |
| :--- | :---: | :--- |
| `README.md` | Human / Coordinator | Quick-start, preset guide, launch prompts, and morning review flow |
| `POLICY.md` | Human | Quota presets, wave budgets, circuit breakers, forbidden files, and deep-track rules |
| `SWARM_PLAYBOOK.md` | Coordinator | Step-by-step single- & multi-wave `/swarm` execution protocol, Gemini 4 pinning, and brief templates |
| `zones.json` | Coordinator | Canonical 11-zone disjoint map of `ism-frappe` and `ism-agent-os` with audit rotation state |
| `backlog.json` | Coordinator | Master issue ledger (`open` / `in_progress` / `awaiting_review` / `fixed` / `wontfix` / `deferred`) with fingerprint deduplication |
| `metrics_history.jsonl` | Coordinator | Append-only per-run metrics snapshots (LOC hotspots, static signals, open backlog trend) |
| `runs/` | Coordinator | Immutable morning-review dossiers (`YYYY-MM-DD_run-NNN.md`) + `_TEMPLATE.md` |
| `rfcs/` | Coordinator | Architect design specs (`CH-NNN.md`) for Critical/High `safe_for_auto_fix: false` items + `_TEMPLATE.md` |
| `scripts/health_cli.py` | Shared (CLI) | Zero-dependency Python scanner, multi-wave disjoint batch planner, integration-gate builder, and state mutator |
| `scripts/test_health_cli.py` | Shared (Tests) | Unit test suite for scanner rules, zone disjointness, deduplication, multi-wave batching, and integration gate |

> **Single-Writer Rule:** Only the Swarm `coordinator` modifies files inside `code-health/` (`zones.json`, `backlog.json`, `metrics_history.jsonl`, `runs/*.md`, `rfcs/*.md`). Parallel `workspace: "share"` workers never edit `code-health/` directly, guaranteeing zero merge conflicts on state files.

---

## Preset Lineup

| Preset | Waves | Auditors / Wave | Fixers / Wave | Verifiers | Deep Tracks & Final Gate | Est. Duration |
| :--- | :---: | :---: | :--- | :--- | :--- | :---: |
| **`eco`** | 1 | 1 (1 zone) | 1 (`slot-1`) | 1 (`slot-3`) | None | ~12–15 min |
| **`standard`** | 1 | 1 (2 zones) | 2 (`slot-1..2`) | 1 (`slot-3`) | None | ~20 min |
| **`audit-only`** | 1 | 2 (3 zones) | 0 | 1 (read-only) | None | ~15–20 min |
| **`extended`** | 2 | 2 (3–4 zones total) | Up to 3 Frappe (`slot-1..3`) + 1 `agent-os` | 2 (`slot-4..5`) | Wave 1 `architect` RFC + Wave 2 optional `test-hardening` + **final cross-zone gate** | ~45–60 min |
| **`overnight`** | 4 *(pipelined)* | 2 (6–8 zones total) | Up to 3 Frappe (`slot-1..3`) + 1 `agent-os` | 2 (`slot-4..5`) | `architect` RFC + `static-or-test-sweeper` + `§4` `refactor` + **final cross-zone gate** | ~90–150 min |

> **Want 3–6+ hours overnight?** Chain two `overnight` (or `extended`) sessions via `/schedule` (e.g., 11:30 PM + 2:00 AM on the same nightly branch) rather than exceeding 4 waves in one coordinator context; `backlog.json`, `zones.json`, and `rfcs/` persist all progress automatically between sessions.

---

## Nightly Launch Workflow (2 Steps)

### 1. Create a fresh nightly branch from `main`
```powershell
git checkout main
git checkout -b nightly/$(Get-Date -Format 'yyyy-MM-dd')
```
*(The Swarm coordinator refuses to run code edits if you are still on `main`.)*

### 2. Launch a Swarm session (`/swarm`) with one of the copy-paste prompts below

#### Overnight Marathon *(Before Sleep — 4 Pipelined Waves, All 5 Bench Slots + Deep Tracks, ~90–150 min)*
```text
/swarm /model-triage
Run an Overnight Marathon code-health session following code-health/SWARM_PLAYBOOK.md and code-health/POLICY.md (preset: overnight).
1. Run `preflight`, `scan --seed --record-metrics`, and `init-run --preset overnight`.
2. Execute up to 4 pipelined waves (`next-batch --preset overnight --wave <1..4> --json`), re-running `next-batch` fresh after each wave lands so Wave k+1 can fix newly cross-verified Wave k findings:
   - Saturate all 5 bench slots: up to 3 disjoint Frappe fixers (`slot-1..3`) + 1 `ism-agent-os` fixer (0 slots), verified in parallel by 2 cross-family verifiers (`slot-4`, `slot-5`), alongside 2 parallel read-only auditors (`auditor-1`, `auditor-2`).
   - Run the wave-scheduled deep tracks (`architect` read-only RFCs -> `code-health/rfcs/`, Wave 2 `static-or-test-sweeper`, Wave 3 `POLICY.md §4` file-size `refactor`).
   - Land verifier-approved commits onto this active nightly branch and update `backlog.json`/`zones.json` before killing each wave's `share` workers; obey all `POLICY.md §2` session caps and circuit breakers.
3. After the final wave, run `health_cli.py integration-gate --zones <touched> --slot 4` across all touched zones + `test_cross_track_seams` (reverting any regressing commit if needed), write today's tiered morning dossier under `code-health/runs/`, and commit `code-health/`.
```

#### Extended Night *(Mid-Length — 2 Waves, All 5 Bench Slots + RFC / Test Track, ~45–60 min)*
```text
/swarm /model-triage
Run an Extended nightly code-health session following code-health/SWARM_PLAYBOOK.md and code-health/POLICY.md (preset: extended).
1. Run `preflight`, `scan --seed --record-metrics`, and `init-run --preset extended`.
2. Execute 2 pipelined waves (`next-batch --preset extended --wave <1..2> --json`) using 2 parallel auditors, up to 3 Frappe fixers (`slot-1..3`) + 1 `ism-agent-os` fixer, 2 cross-family verifiers (`slot-4`, `slot-5`), and the optional Wave 1 `architect` RFC / Wave 2 `test-hardening` tracks.
3. Land only verifier-approved commits onto this active nightly branch before killing each wave's workers, run `health_cli.py integration-gate --zones <touched> --slot 4` after Wave 2, update ledgers/RFCs, and write today's tiered morning dossier under `code-health/runs/`.
```

#### Standard Night *(Quick — 1 Wave, 1 Auditor + 2 Disjoint Fixers + 1 Verifier, ~20 min)*
```text
/swarm /model-triage
Run a Standard nightly code-health session following code-health/SWARM_PLAYBOOK.md and code-health/POLICY.md (preset: standard).
1. Verify we are on a non-main branch via `python code-health/scripts/health_cli.py preflight`.
2. Run `scan --seed --record-metrics` and `next-batch --preset standard --json` to pick 2 due audit zones and 2 strictly disjoint fix tracks.
3. Spawn monitor, auditor (read-only), fixer-a (workspace: share), fixer-b (workspace: share), and cross-family verifier per SWARM_PLAYBOOK.md.
4. Merge only verifier-approved commits onto this active nightly branch before killing workers, ingest new auditor findings into backlog.json, update zones.json, and write today's morning dossier under code-health/runs/.
```

#### Eco Night *(Lowest Quota — 1 Wave, 1 Auditor + 1 Fixer + 1 Verifier, ~12–15 min)*
```text
/swarm /model-triage
Run an Eco nightly code-health session following code-health/SWARM_PLAYBOOK.md and code-health/POLICY.md (preset: eco).
Keep total code changes <= 250 LOC on this active nightly branch, verify with cross-family verifier, and write today's morning dossier under code-health/runs/.
```

#### Deep Audit Night *(Read-Only — Zero Code Edits, Zero Bench Slots, ~15–20 min)*
```text
/swarm /model-triage
Run a Read-Only Deep Audit code-health session following code-health/SWARM_PLAYBOOK.md and code-health/POLICY.md (preset: audit-only).
Audit the top 3 due zones in parallel using ast-outline and frappe-code-review, populate backlog.json with cross-verified findings, update zones.json, and write today's dossier under code-health/runs/. Do not edit application code.
```

---

## Morning Review Workflow (5–10 Minutes)

1. Open the newest dossier in `code-health/runs/YYYY-MM-DD_run-NNN.md` — check **Low-Risk / Auto-Merge Ready** commits vs **Careful Review** commits and any new **Architect RFCs** under `code-health/rfcs/`.
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
   *(If you only want a subset of commits, `git cherry-pick` the verified commits you want plus the `code-health/` ledger commit, or `git revert` an individual commit cleanly.)*

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

# Compute disjoint work packets for Swarm (single- or multi-wave)
python code-health/scripts/health_cli.py next-batch --preset overnight --wave 1 --json
python code-health/scripts/health_cli.py next-batch --preset extended --wave 2 --exclude-zones frappe-portal-booking --json

# Build final cross-zone integration gate commands for all touched zones
python code-health/scripts/health_cli.py integration-gate --zones frappe-agent-facade,frappe-repair-order,agent-os-core --slot 4 --json

# Initialize today's run dossier from template
python code-health/scripts/health_cli.py init-run --preset overnight

# Add / update backlog items & mark zones audited
python code-health/scripts/health_cli.py add-issue --from-json path/to/issues.json
python code-health/scripts/health_cli.py update-issue --id CH-001 --status fixed --resolved-run run-003 --commit <sha>
python code-health/scripts/health_cli.py mark-audited --zone frappe-scheduling --run run-003
```
