# Nightly Code-Health Swarm Playbook

Drop-in orchestration protocol for Antigravity `/swarm` + `/model-triage` sessions operating on `d:\ism\code-health\`.

---

## 1. Swarm Topology & Gemini 4 Pinning (`model-triage`)

Before the first `swarm{action:"add"}`, call `swarm{action:"models", all:true}` once and verify `quotas` / `quota_note` across `gemini-4-hydrogen-*`, `gemini-4-neon-*`, and `gemini-4-carbon-*` (never select `argon`, `gemini-3.8-*`, `claude`, `gpt-oss`, or `orchestrator`).

| Worker | Role & Scope | Workspace | Bench Slot | Model Pin | Escalation Path |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **`coordinator`** | Runs `health_cli.py`, drives wave loop (`1..W`), updates `plan.json`, lands verified commits, owns all writes to `code-health/` (including `rfcs/`) | `inherit` | *(gate: `slot-4`)* | `Neon (Medium)` *(single-wave)* or `Neon (High)` *(`extended`/`overnight`)* | `swarm{action:"model"}` → `neon-high` |
| **`monitor`** | Standard `swarm_monitor` stall watcher (stays alive across all waves) | `inherit` | — | `gemini-4-hydrogen-low` | — |
| **`auditor`** *(or `auditor-1`, `auditor-2`)* | Read-only semantic audit of 1–2 due zones via `ast-outline` + `frappe-code-review`; triages static signals and posts structured JSON findings | `inherit` | — | `gemini-4-neon-medium` *(use `gemini-4-neon-high` for `Critical` zones)* | `neon-high` |
| **`architect`** *(`extended` W1; `overnight` W1/W3)* | Read-only design spec author for top 1–2 Critical/High `safe_for_auto_fix: false` items; posts RFC markdown matching `code-health/rfcs/_TEMPLATE.md` | `inherit` | — | `gemini-4-neon-high` | `carbon-high` |
| **`fixer-a`..`fixer-c`** | Disjoint Frappe zones (`semantic-fix`, `static-sweeper`, `test-hardening`, or `§4` `refactor`), commits cleanly in its isolated worktree | `share` | **`slot-1`..`slot-3`** | `gemini-4-hydrogen-medium` *(tests/sweeper/small)* or `gemini-4-neon-medium` *(security/perf/refactor)* | `neon-medium` → `neon-high` |
| **`fixer-d`** *(`extended` / `overnight`, when `agent-os` eligible)* | Disjoint `ism-agent-os` zone (`agent-os-core` or `agent-os-skills-evals`), verified via `python -m unittest` | `share` | **None (`0` slots)** | `gemini-4-hydrogen-medium` or `gemini-4-neon-medium` | `neon-medium` |
| **`verifier`** *(`eco`/`standard`)* or **`verifier-1`, `verifier-2`** *(`extended`/`overnight`)* | Cross-family independent code + test gate AND read-only fact-check of auditor findings | `inherit` | **`slot-3`** *(single)* or **`slot-4`, `slot-5`** *(parallel)* | `gemini-4-neon-high` *(for `hydrogen-*` fixers)* or `gemini-4-carbon-high` *(for `neon-*` fixers & audit checks)* | `carbon-high` / `neon-xhigh` |

> **Startup Output:** Emit one compact table (`worker | role | zone | model | slot | why`) and write `<appData>/brain/<conv-id>/.system_generated/swarm/plan.json` per the `swarm_pane` skill. Do not add a `narrator`.

### Multi-Wave Deep-Track Schedule (`extended` & `overnight`)
`health_cli.py next-batch --preset <preset> --wave <k> --json` automatically assigns wave-specific tracks without exceeding `slot-1..3` for Frappe fixers:

- **`extended` (2 waves, ~45–60 min):**
  - **Wave 1:** `auditor-1..2` (2 zones) + up to 3 Frappe fixers (`slot-1..3`) + 1 `agent-os` fixer + `architect` (RFC for 1–2 Critical/High blockers) + `verifier-1..2` (`slot-4..5`).
  - **Wave 2:** `auditor-1..2` (next 1–2 zones) + pipelined semantic fixers + optional `test-hardening` track + `verifier-1..2` → **Final Cross-Zone Integration Gate**.
- **`overnight` (4 waves, ~90–150 min):**
  - **Wave 1:** `auditor-1..2` (2 zones) + semantic fixers (`slot-1..3` + `agent-os`) + `architect` RFC track + `verifier-1..2` (`slot-4..5`).
  - **Wave 2:** `auditor-1..2` (next 2 zones) + 2 semantic fixers (including Wave 1 pipelined findings!) + **1 `static-or-test-sweeper`** + `agent-os` + `verifier-1..2`.
  - **Wave 3:** `auditor-1..2` (next 2 zones) + 2 semantic fixers + **1 `POLICY.md §4` file-size `refactor`** (`<= 800` move-only LOC) + `agent-os` + optional `architect` RFC + `verifier-1..2`.
  - **Wave 4:** `auditor-1..2` (next 2 zones, up to 8 total) + 3 semantic fixers draining Wave 2–3 findings + `agent-os` + `verifier-1..2` → **Final Cross-Zone Integration Gate**.

---

## 2. Coordinator Step-by-Step Lifecycle

### Step 1 — Preflight & Run Initialization (0 LLM tokens)
Run in the coordinator workspace:
```powershell
python code-health/scripts/health_cli.py preflight
python code-health/scripts/health_cli.py scan --seed --record-metrics --json
python code-health/scripts/health_cli.py init-run --preset <eco|standard|audit-only|extended|overnight> --json
```
- **Hard Stop:** If `preflight` exits non-zero (e.g., on `main` or dirty non-ledger working tree), **stop immediately** and ask the user to check out a clean nightly branch.
- Record `run_id`, active branch name, base `HEAD` SHA, and `session_limits` from the preset.
- Spawn `monitor` (`gemini-4-hydrogen-low`) once at session start; keep it alive across all waves until final shutdown.
- Call `swarm{action:"title", title:"Code Health <run_id> (<preset>)"}` and write `.system_generated/swarm/plan.json`.

### Step 2 — Wave Loop (`wave = 1 .. total_waves`)
Repeat for each wave until `wave > total_waves` OR a `POLICY.md §2` circuit breaker triggers:

#### 2a. Plan Wave Batch (Re-Planned Fresh After Prior Wave Updates!)
```powershell
python code-health/scripts/health_cli.py next-batch --preset <preset> --wave <k> --json
```
- *(Pass `--exclude-zones <csv>` if a zone was deferred due to an unresolved failure earlier in the same night.)*
- Mark selected fix items `in_progress` via `health_cli.py update-issue --id <ID> --status in_progress`.
- Update `.system_generated/swarm/plan.json` for Wave `k`.

#### 2b. Spawn Wave Workers with Strictly Disjoint Scopes
- Spawn fresh workers for Wave `k` (e.g., `auditor-1`, `auditor-2`, `fixer-a`..`fixer-d` with `workspace: "share"` branched from the **current updated `HEAD`**, `verifier-1`, `verifier-2`, and optional `architect` when `deep_tracks` includes `rfc`).
- *(Tip: In multi-wave runs, you may reuse read-only `verifier-1`/`verifier-2` across waves if their context is still light, or spawn fresh `w<k>-*` workers per wave; ALWAYS kill and re-spawn `workspace: "share"` fixers each wave so every fixer branches cleanly from the latest merged `HEAD`!)*
- Every worker brief MUST state:
  1. Exact **Zone ID**, **track kind** (`semantic-fix` / `static-sweeper` / `test-hardening` / `refactor`), and **allowed file paths** (plus explicit ban on editing outside that zone or inside `code-health/`).
  2. Exact **assigned bench slot** (`-Slot 1..3` for Frappe fixers, `-Slot 4..5` for parallel verifiers, `-Slot 3` for single verifier, or `unittest` for `ism-agent-os`).
  3. Exact **LOC cap** and **retry cap** (`2`).
  4. Exact **done output format** (commit SHA + worktree path + diff stat + test output for fixers; `NEW_FINDINGS_JSON` + `STATIC_TRIAGE_JSON` for auditors; RFC markdown for `architect`).

#### 2c. Parallel Cross-Family Verification & Retry Gate
- Route each fixer's `READY_FOR_VERIFY` report to its assigned verifier (`verifier-1` on `slot-4` or `verifier-2` on `slot-5`; never run two commands on the same slot simultaneously).
- Route auditor `NEW_FINDINGS_JSON` and `STATIC_TRIAGE_JSON` to a cross-family verifier (`gemini-4-carbon-high`) for line-by-line fact-checking.
- **If PASS:** Proceed to Step 2d.
- **If FAIL:** Return exact failure reasons to the fixer (max 2 retries; escalate model via `swarm{action:"model"}` on retry #2). If still failing, mark item `deferred` with diagnostic notes, increment the consecutive-failure counter, and **do not merge**.

#### 2d. Wave Commit Landing, Pipelining & Cleanup (CRITICAL Before Killing Workers)
> **CRITICAL:** `swarm{action:"kill"}` permanently deletes a `workspace: "share"` checkout. Always land verified commits into git history **before** killing any wave fixer!

1. Cherry-pick each **verifier-approved** fixer commit onto the active nightly branch (`git cherry-pick <sha>`), confirming cleanliness after each pick. If an unexpected conflict ever occurs, `git cherry-pick --abort`, preserve as `git branch nightly/<date>-w<k>-<worker>-<zone> <sha>`, and note it in the dossier.
2. **Kill finished wave `share` fixers** (and wave auditors/architect) only after their commits/outputs are safely captured.
3. **Update ledger in-place so Wave `k+1` sees fresh state (Audit → Fix Pipelining!):**
   - Save cross-verified auditor findings to `<appData>/brain/<conv-id>/scratch/w<k>_audit_findings.json` and run `health_cli.py add-issue --from-json ... --run <run_id>`.
   - Apply verified `wontfix` / escalation static triages and mark landed items `fixed` via `health_cli.py update-issue --id <ID> --status fixed --resolved-run <run_id> --commit <landed_sha>`.
   - Mark audited zones via `health_cli.py mark-audited --zone <zone_id> --run <run_id>`.
   - Save any `architect` RFC markdown into `code-health/rfcs/<CH-ID>.md` (coordinator single-writer).
4. **Check Circuit Breakers (`POLICY.md §2`):**
   - Check cumulative commits vs `max_session_commits`, cumulative net LOC vs `max_session_loc`, consecutive failures (`>= 2`), and remaining model quota. If any breaker tripped, end the wave loop early and record the reason in the dossier.

### Step 3 — Final Cross-Zone Integration Gate (`extended` & `overnight`)
Once the wave loop completes (if any application commits landed on the branch):
```powershell
python code-health/scripts/health_cli.py integration-gate --zones <comma-separated-touched-zones> --slot 4 --json
```
1. Execute the returned single batched Frappe `test-in-slot.ps1 -Slot 4 -Module ...,ism.tests.test_cross_track_seams` command against the merged nightly branch `HEAD`.
2. If any `ism-agent-os` zone was touched, execute `python -m unittest discover -s ism-agent-os/tests -t ism-agent-os -v`.
3. **If the cross-zone gate fails:** Identify the regressing commit, revert ONLY that commit with `git revert --no-edit <sha>`, update its backlog item(s) to `deferred` with the cross-zone traceback, and re-run the integration gate until green.

### Step 4 — Finalize Metrics, Tiered Morning Dossier & Ledger Commit
1. Refresh post-run metrics snapshot:
   ```powershell
   python code-health/scripts/health_cli.py scan --record-metrics
   python -m unittest code-health/scripts/test_health_cli.py
   ```
2. Populate `code-health/runs/YYYY-MM-DD_<run_id>.md` using `_TEMPLATE.md` with:
   - Executive KPI table (waves completed, circuit-breaker status, zones audited, new verified findings, items fixed/deferred, RFCs written)
   - **Tiered Morning Review tables:** *Low-Risk / Auto-Merge Ready* (tests, static sweepers, small fixes) vs *Careful Review* (security, billing/payments, `§4` refactors)
   - **Architect RFCs Generated** with 1-line decision prompts
   - **Final Cross-Zone Integration Gate** proof block
   - Ready-to-run morning merge & selective cherry-pick commands
3. Commit all `code-health/` updates onto the active nightly branch:
   ```powershell
   git add code-health/
   git commit -m "chore(code-health): record <run_id> audit, RFCs & ledger updates"
   ```
4. Kill any remaining workers (including `monitor`), verify `git status` is clean, and post a compact morning summary to the user.

---

## 3. Worker Brief Templates

### Auditor Brief Template (`workspace: "inherit"`, Read-Only)
```text
You are `<auditor|auditor-1|auditor-2>` for Code Health <run_id> (Wave <k>/<W>). Your role is strictly READ-ONLY — do NOT modify any files.
Assigned zone(s): <zone_ids>
Allowed paths to inspect: <zone_paths>
High-signal static items to triage: <static_summary>

Instructions:
1. Read `d:\ism\.agents\skills\frappe-code-review\SKILL.md` (for `ism-frappe` zones) and use `ast-outline` before full file reads.
2. Focus on high-signal semantic issues static linters miss:
   - Permission / role / guest-endpoint boundary leaks (`ignore_permissions=True`, missing role/ownership checks)
   - Race conditions, missing `FOR UPDATE` locks, non-idempotent submit/cancel/webhook flows
   - Financial/estimation math drift, stock/billing invariants
   - Query performance (N+1 loops, unbounded queries, heavy `on_update` hooks)
   - Missing edge-case test coverage & scoped `POLICY.md §4` modularization plans
3. Skip anything already in `code-health/backlog.json` (active or `wontfix`), EXCEPT triaging the listed static items.
4. Post on the board mentioning `@<verifier>` and `@coordinator`:
   - `audited_zones`: [...]
   - `NEW_FINDINGS_JSON` array (`title`, `rule`, `category`, `severity`, `zone`, `files`, `line_hint`, `symbol`, `safe_for_auto_fix`, `estimated_effort`, `evidence`, `proposed_fix`, `verification_cmd`)
   - `STATIC_TRIAGE_JSON` array (`id`, `action`: `wontfix|keep-open|escalate`, `rationale`)
   - 3-bullet health summary per zone.
```

### Fixer Brief Template (`workspace: "share"` — Semantic / Sweeper / Test-Only / Refactor)
```text
You are `<fixer-a|fixer-b|fixer-c|fixer-d>` for Code Health <run_id> (Wave <k>/<W>, track_kind: <semantic-fix|static-sweeper|test-hardening|refactor>).
Assigned zone: <zone_id>
Bench slot: <slot-1..3 OR None (unittest)>
Allowed write paths: <zone_paths + disjoint test file(s)>
Assigned item(s): <CH-IDs and descriptions>
Verification command: <exact test-in-slot.ps1 -Slot <N> ... OR python -m unittest ...>

Hard guardrails (`code-health/POLICY.md`):
- NEVER edit files outside your assigned zone or inside `code-health/`.
- NEVER touch DocType `.json` schemas, `patches.txt`, `hooks.py` routes, or `development.localhost:8000`.
- Keep total diff <= <250|400|450|800> LOC.
- Track-specific rules:
  - If `test-hardening`: ZERO edits to production code; add tests only. Document any newly uncovered production bug instead of weakening assertions.
  - If `static-sweeper`: Fix genuine N+1/unbounded/swallowed-exception items cleanly; list any verified false positives as `PROPOSED_WONTFIX` with line proof instead of forcing noisy edits.
  - If `refactor` (`POLICY.md §4`): Max 1 file split; re-export ALL public symbols/whitelisted paths from the original module so zero imports/routes break.
- Run your exact verification command in ONE batched invocation until green (max 2 retries; stop and report if blocked).
- Commit verified changes in your worktree with a conventional commit (`fix(<zone>): ...`, `test(<zone>): ...`, or `refactor(<zone>): ...`).

Done format on board (mention `@<verifier>` and `@coordinator`):
- Status: READY_FOR_VERIFY | BLOCKED
- Worktree Path: <git rev-parse --show-toplevel>
- Commit SHA: <full sha>
- Item IDs addressed: <CH-IDs>
- Diff stat: <git diff --stat HEAD~1..HEAD>
- Verification command & exact pass/fail summary
- Proposed `wontfix` / newly discovered issues (if any)
```

### Architect RFC Brief Template (`workspace: "inherit"`, Read-Only)
```text
You are `architect` for Code Health <run_id> (Wave <k>/<W>). Your role is strictly READ-ONLY — do NOT modify or create any files anywhere in the repo (coordinator writes `code-health/rfcs/`).
Assigned Critical/High architectural item(s): <CH-IDs, files, evidence>

Instructions:
1. Read `code-health/rfcs/_TEMPLATE.md` and inspect every relevant file and caller via `ast-outline` (`grep`, `show`, `outline`) before full reads.
2. For each assigned item, produce a concrete, code-verified RFC following `code-health/rfcs/_TEMPLATE.md`:
   - Verified callsite & blast-radius inventory (exact paths + line numbers)
   - 2–3 concrete design options with explicit security/data/migration trade-offs
   - Recommended step-by-step implementation & test plan (marking any DocType/hook/frontend touchpoints)
   - Crisp 2-line Morning Decision Prompt (e.g., "Approve Option A vs Option B?")
3. Post each RFC on the board mentioning `@coordinator` inside fenced blocks labeled `RFC_MARKDOWN:<CH-ID>`.
```

### Verifier Brief Template (`workspace: "inherit"`, Cross-Family)
```text
You are `<verifier|verifier-1|verifier-2>` (cross-family independent gate) for Code Health <run_id>. Do NOT edit production code.
Assigned bench slot: <-Slot 3 | -Slot 4 | -Slot 5> (run strictly ONE slot command at a time).
Assigned fixers: <fixer list>

For each fixer commit `<sha>` (after verifying worktree `HEAD == <sha>` and `git -C <worktree> status --short -- ism-frappe/ ism-agent-os/` is clean):
1. Inspect `git show --stat <sha>` and `git diff <sha>~1..<sha>`.
2. Enforce `code-health/POLICY.md`:
   - All changed files stay inside the fixer's assigned zone (ZERO edits to `code-health/`, DocType `.json`, `patches.txt`, `hooks.py`).
   - Total changed LOC within track cap (`test-hardening`: zero production edits; `refactor`: all public symbols/whitelisted paths preserved).
   - No weakened test assertions, no `ism-agent-os/rules/AGENTS.md >= 24,000` bytes.
3. Independently run the zone's verification command against the fixer's worktree on your assigned slot (never `development.localhost`).
4. Post `VERDICT: PASS` or `VERDICT: FAIL` per fixer mentioning `@coordinator` (and the fixer if FAIL) with exact test counts and any defects.

When assigned an auditor report, independently verify every proposed finding and `wontfix` triage against source code and post `AUDIT_VERIFICATION` mentioning `@coordinator`.
```
