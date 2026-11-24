# Nightly Code-Health Swarm Playbook

Drop-in orchestration protocol for Antigravity `/swarm` + `/model-triage` sessions operating on `d:\ism\code-health\`.

---

## 1. Swarm Topology & Gemini 4 Pinning (`model-triage`)

Before the first `swarm{action:"add"}`, call `swarm{action:"models", all:true}` once and verify `quotas` / `quota_note` across `gemini-4-hydrogen-*`, `gemini-4-neon-*`, and `gemini-4-carbon-*` (never select `argon`, `gemini-3.8-*`, `claude`, `gpt-oss`, or `orchestrator`).

| Worker | Role & Scope | Workspace | Model Pin | Escalation Path |
| :--- | :--- | :---: | :--- | :--- |
| **`coordinator`** | Runs `health_cli.py`, picks disjoint batch, updates Swarm Overview `plan.json`, merges verified commits onto active nightly branch, owns all writes to `code-health/` | `inherit` | `Gemini 4 Neon (Medium)` *(or `Neon (High)`)* | `swarm{action:"model"}` → `neon-high` |
| **`monitor`** | Standard `swarm_monitor` stall watcher | `inherit` | `gemini-4-hydrogen-low` | — |
| **`auditor`** *(or `auditor-1..N` in `audit-only`)* | Read-only semantic audit of 1–2 due zones via `ast-outline` + `frappe-code-review`; posts structured JSON findings on board | `inherit` | `gemini-4-neon-medium` *(use `gemini-4-neon-high` for `Critical` security/payments/portal zones)* | `neon-high` |
| **`fixer-a`** | Disjoint **Zone X** from `next-batch`; implements 1–2 fixes or tests, runs zone tests on **`slot-1`** (or `unittest` for `ism-agent-os`), commits cleanly in its worktree | `share` | `gemini-4-hydrogen-medium` *(tests/hygiene/small fixes)* or `gemini-4-neon-medium` *(N+1/perf/refactor)* | `neon-medium` → `neon-high` |
| **`fixer-b`** *(Standard preset)* | Disjoint **Zone Y** from `next-batch`; implements 1–2 fixes or tests, runs zone tests on **`slot-2`** (or `unittest`), commits cleanly in its worktree | `share` | `gemini-4-hydrogen-medium` or `gemini-4-neon-medium` | `neon-medium` → `neon-high` |
| **`verifier`** | Cross-family review + independent verification (`slot-3` for `ism-frappe` / `unittest` for `ism-agent-os`), checks diff caps & `POLICY.md` invariants | `inherit` | `gemini-4-neon-high` *(when verifying `hydrogen-*` fixers)* or `gemini-4-carbon-high` *(when verifying `neon-*` fixers)* | `carbon-high` / `neon-xhigh` |

> **Startup Output:** Emit one compact table (`worker | role | zone | model | slot | why`) and write `<appData>/brain/<conv-id>/.system_generated/swarm/plan.json` per the `swarm_pane` skill. Do not add a `narrator`.

---

## 2. Coordinator Step-by-Step Lifecycle

### Step 1 — Preflight & Deterministic Batch Selection (0 LLM tokens)
Run in the coordinator workspace:
```powershell
python code-health/scripts/health_cli.py preflight
python code-health/scripts/health_cli.py scan --seed --record-metrics --json
python code-health/scripts/health_cli.py next-batch --preset <eco|standard|audit-only> --json
python code-health/scripts/health_cli.py init-run --preset <eco|standard|audit-only>
```
- **Hard Stop:** If `preflight` exits non-zero (e.g., currently on `main` or dirty working tree with unrelated edits), **stop immediately** and ask the user to check out a clean nightly branch.
- Record the `run_id`, active branch name, and base `HEAD` SHA.
- Mark selected fix items `in_progress` via `health_cli.py update-issue --id <ID> --status in_progress` so the state reflects active work.

### Step 2 — Spawn Workers with Disjoint Scopes
- Spawn `monitor` (`gemini-4-hydrogen-low`), `auditor` (`inherit`), and — unless `audit-only` — `fixer-a` (`share`), optional `fixer-b` (`share`), and `verifier` (`inherit`).
- Every worker brief MUST state:
  1. Exact **Zone ID** and **allowed file paths** (and explicit ban on editing outside that zone or editing `code-health/`).
  2. Exact **assigned bench slot** (`-Slot 1` for `fixer-a`, `-Slot 2` for `fixer-b`, `-Slot 3` for `verifier`, or `unittest` if `ism-agent-os`).
  3. Exact **LOC cap** (`<= 250` for `eco`, `<= 400` for `standard`) and **retry cap** (max 2).
  4. Exact **done output format** (commit SHA + diff stat + test command output for fixers; JSON array matching `add-issue` schema for `auditor`).
- Call `swarm{action:"title", title:"Code Health <run_id> (<preset>)"}` and write `.system_generated/swarm/plan.json`.

### Step 3 — Verification & Retry Gate
- As each fixer reports its commit SHA + diff stat + local test proof:
  - Ask `verifier` to inspect the commit (`git show --stat <sha>` and `git diff <base>..<sha>`), check all `POLICY.md` prohibitions (no DocType JSONs, no `hooks.py` breakage, LOC cap met, no weakened tests), and independently execute the zone's verification command (using `-Slot 3` on a temporary checkout or verifying against the worker's worktree path without touching `development.localhost`).
  - **If PASS:** Proceed to Step 4 for that fixer.
  - **If FAIL:** Send exact failure reasons back to the fixer (up to 2 attempts; escalate model via `swarm{action:"model"}` on second attempt if needed). If still failing after 2 retries, mark the item `deferred` with diagnostic notes and **do not merge** its commit.

### Step 4 — Safe Landing Before Killing Workers
> **CRITICAL:** `swarm{action:"kill"}` permanently deletes a `workspace: "share"` checkout. Always land verified commits into the shared git history **before** killing any worker!

For each **verifier-approved** fixer commit `<sha>`:
1. Verify cleanliness in coordinator's active nightly branch (`git status --short`).
2. Merge or cherry-pick the verified commit onto the active nightly branch:
   ```powershell
   git cherry-pick <sha>
   ```
   *(Because `fixer-a` and `fixer-b` own strictly disjoint zones, cherry-picking both onto the active nightly branch is conflict-free. If an unexpected conflict ever occurs, abort with `git cherry-pick --abort`, save the commit as a standalone review branch `git branch nightly/<date>-<worker>-<zone> <sha>`, and note it in the morning dossier.)*
3. Confirm the commit is present via `git log -n 3 --oneline`.
4. Now it is safe to `swarm{action:"kill"}` the finished worker.

For any **unverified / partial** work worth preserving for human inspection (optional), create a named branch `git branch nightly/<date>-<worker>-partial <sha>` before killing the worker, and never cherry-pick it onto the active nightly branch.

### Step 5 — Persist Memory & Render Morning Dossier
1. **Ingest auditor discoveries:** Save the auditor's validated JSON array to a temp file in `<appData>/brain/<conv-id>/scratch/audit_findings.json` (never leaving stray temp files in the repo) and run:
   ```powershell
   python code-health/scripts/health_cli.py add-issue --from-json "<scratch_path>" --run <run_id>
   ```
2. **Update zone rotation timestamps:**
   ```powershell
   python code-health/scripts/health_cli.py mark-audited --zone <zone_id> --run <run_id>
   ```
3. **Update fixed / deferred item statuses:**
   ```powershell
   python code-health/scripts/health_cli.py update-issue --id <CH-ID> --status fixed --resolved-run <run_id> --commit <landed_sha>
   ```
4. **Refresh post-run metrics snapshot:**
   ```powershell
   python code-health/scripts/health_cli.py scan --record-metrics
   ```
5. **Finalize `code-health/runs/YYYY-MM-DD_<run_id>.md`** with:
   - Executive KPI table (zones audited, new issues discovered, items fixed/deferred, commits landed)
   - Verification proof table (exact test commands & pass counts)
   - New High/Critical findings & "Needs Human Decision" items
   - Ready-to-run morning merge command (`git checkout main && git merge --no-ff <branch> && ...`)
6. **Commit the ledger & dossier** onto the active nightly branch:
   ```powershell
   git add code-health/
   git commit -m "chore(code-health): record <run_id> audit & ledger updates"
   ```
7. Kill remaining non-monitor workers (and `monitor`), verify `git status` is clean, and post a compact morning summary to the user.

---

## 3. Worker Brief Templates

### Auditor Brief Template (`workspace: "inherit"`, Read-Only)
```text
You are `auditor` for Code Health <run_id>. Your role is strictly READ-ONLY — do NOT modify any files.
Assigned zone(s): <zone_ids>
Allowed paths to inspect: <zone_paths>
Static signals already flagged in this zone: <static_summary>

Instructions:
1. Read `d:\ism\.agents\skills\frappe-code-review\SKILL.md` (for `ism-frappe` zones) and use `ast-outline` before full file reads.
2. Focus on high-signal semantic issues static linters miss:
   - Permission / tenant / guest-endpoint boundary leaks (`ignore_permissions=True`, missing role/ownership checks)
   - Race conditions, missing `FOR UPDATE` locks, non-idempotent submit/cancel/webhook flows
   - Query performance (N+1 loops, unbounded queries, heavy `on_update` hooks)
   - Missing edge-case test coverage in critical paths
   - High-leverage modularization opportunities (note prerequisites/test coverage)
3. Skip anything already in `code-health/backlog.json` (active or `wontfix`).
4. Report ONLY concrete, verified findings with exact file paths and line numbers. Post a single JSON array on the board where each element has:
   `title`, `category` (`security|performance|correctness|file-size|test-coverage|hygiene`), `severity` (`critical|high|medium|low`), `zone`, `files`, `line_hint`, `symbol`, `safe_for_auto_fix` (bool), `estimated_effort` (`small|medium|large`), `evidence`, `proposed_fix`, `verification_cmd`.
   Also list `audited_zones` and a 3-bullet summary of overall zone health.
```

### Fixer Brief Template (`workspace: "share"`)
```text
You are `<fixer-a|fixer-b>` for Code Health <run_id>.
Assigned zone: <zone_id>
Allowed write paths: <zone_paths>
Assigned item(s): <CH-IDs and descriptions>
Verification command: <exact test-in-slot.ps1 -Slot <N> ... OR python -m unittest ...>

Hard guardrails (`code-health/POLICY.md`):
- NEVER edit files outside your assigned zone or inside `code-health/`.
- NEVER touch DocType `.json` schemas, `patches.txt`, `hooks.py` routes, or `development.localhost:8000`.
- Keep total diff <= <250|400> LOC. Max 1 file split if doing file-size reduction, preserving all public symbols.
- If a newly added test reveals a real production bug outside your item scope, document the bug in your report rather than weakening assertions.
- Run your exact verification command until green (max 2 retries; stop and report if blocked).
- Commit your verified changes in your worktree with a clear conventional commit message (`fix(<zone>): ...` or `test(<zone>): ...`).

Done format on board:
- Status: READY_FOR_VERIFY | BLOCKED
- Commit SHA: <full sha>
- Item IDs addressed: <CH-IDs>
- Diff stat: <git diff --stat HEAD~1..HEAD>
- Verification command & exact pass/fail summary
- Follow-up / newly discovered issues (if any)
```

### Verifier Brief Template (`workspace: "inherit"`, Cross-Family)
```text
You are `verifier` (cross-family independent gate) for Code Health <run_id>. Do NOT edit production code.
For each fixer commit assigned by `coordinator`:
1. Inspect `git show --stat <sha>` and `git diff <sha>~1..<sha>`.
2. Enforce `code-health/POLICY.md`:
   - All changed files stay inside the fixer's assigned zone (and zero edits to `code-health/`, DocType `.json`, `patches.txt`, `hooks.py`).
   - Total changed LOC within preset cap.
   - No broken public imports/whitelisted paths, no weakened test assertions, no `ism-agent-os/rules/AGENTS.md >= 24,000` bytes.
3. Independently run the zone's verification command:
   - For `ism-frappe` zones: run `powershell -ExecutionPolicy Bypass -File scripts\test-in-slot.ps1 -Slot 3 -Module <modules>` from the fixer's worktree (never `development.localhost`).
   - For `ism-agent-os` zones: run `python -m unittest ...` from the fixer's worktree.
4. Post `VERDICT: PASS` or `VERDICT: FAIL` per fixer with exact test counts and any policy/correctness defects.
```
