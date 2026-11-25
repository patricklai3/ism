# Code Health Operating Policy & Guardrails

Every nightly or recurring Swarm run MUST obey these guardrails. When a task conflicts with this policy, **stop, document the issue in `backlog.json` (or mark it `deferred`), and escalate to the morning dossier** rather than guessing or loosening safety rules.

---

## 1. Branch & Environment Isolation (Non-Negotiable)

1. **Never edit or commit on `main`.** The user creates and checks out a dedicated branch (e.g., `nightly/YYYY-MM-DD`) before starting a run. `health_cli.py preflight` fails if `HEAD` is on `main` or detached.
2. **Zero `development.localhost:8000` contact.** Never run `sync-to-wsl.ps1`, `watch-sync.*`, `promote-to-main.ps1`, or direct `wsl` / `docker exec` bench commands targeting primary `frappe-bench`.
3. **Isolated Bench Slots Only (`slot-1`..`slot-5`).** Every `ism-frappe` verification command runs strictly via:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\test-in-slot.ps1 -Slot <1..5> -Module <mod1,mod2>
   ```
   Do not run parallel `test-in-slot.ps1` calls inside the same worktree; batch modules comma-separated in one invocation.
   - **Single-wave presets (`eco`, `standard`):** Fixers use `slot-1`..`slot-2`; single `verifier` uses `slot-3`.
   - **Multi-wave presets (`extended`, `overnight`):** Frappe fixers use `slot-1`..`slot-3` (max 3 Frappe writers per wave, plus up to 1 zero-slot `ism-agent-os` writer); parallel verifiers use `slot-4` and `slot-5`.
4. **No remote pushes.** Never `git push` during an autonomous run.
5. **Single-Writer Ledger Ownership.** Only the `coordinator` modifies files under `code-health/` (`zones.json`, `backlog.json`, `metrics_history.jsonl`, `runs/*.md`, `rfcs/*.md`). Fixer, auditor, and architect workers report findings, RFC content, and commit SHAs on the Swarm board.

---

## 2. Quota Presets, Wave Budgets & Blast-Radius Caps

| Preset | Waves | Auditors / Wave | Audit Zones / Run | Fixers / Wave | Verifiers | Max Items / Fixer | Max LOC / Fixer | Deep Tracks & Final Gate | Session Caps | Est. Duration |
| :--- | :---: | :---: | :---: | :--- | :--- | :---: | :---: | :--- | :--- | :---: |
| **`eco`** | 1 | 1 | 1 | 1 (`slot-1`) | 1 (`slot-3`) | 1–2 | `<= 250` | None | `<= 2` commits / `300` LOC | ~12–15 min |
| **`standard`** | 1 | 1 | 2 | 2 (`slot-1..2`) | 1 (`slot-3`) | 1–2 | `<= 400` | None | `<= 4` commits / `800` LOC | ~20 min |
| **`audit-only`** | 1 | 2 | 3 | 0 | 1 (read-only) | 0 | `0` | None | `0` app commits | ~15–20 min |
| **`extended`** | 2 | 2 | 3–4 | Up to 3 Frappe (`slot-1..3`) + 1 `agent-os` | 2 (`slot-4..5`) | 1–2 | `<= 400` | Wave 1: optional `architect` RFC; Wave 2: optional `test-hardening`; **final cross-zone gate** | `<= 8` commits / `2,000` LOC | ~45–60 min |
| **`overnight`** | 4 *(pipelined)* | 2 | 6–8 | Up to 3 Frappe (`slot-1..3`) + 1 `agent-os` | 2 (`slot-4..5`) | 1–3 *(sweeper: up to 6)* | `<= 450` *(refactor: `<= 800` move-only)* | Wave 1 & 3: `architect` RFC; Wave 2: `static-or-test-sweeper`; Wave 3: `§4` file-size `refactor`; **final cross-zone gate** | `<= 14` commits / `3,000` LOC | ~90–150 min |

### Stop Rules & Circuit Breakers
- **Per-Item Retry Stop Rule:** If a fixer fails verification twice on the same item, stop working on it, mark it `deferred` with the failure trace in `backlog.json`, and do NOT burn extra quota looping.
- **Session Circuit Breaker (Multi-Wave Runs):** Stop launching further waves and jump directly to the Final Integration Gate + Morning Dossier if **any** of the following triggers:
  1. **Consecutive failures:** `>= 2` consecutive fixers fail verification across waves.
  2. **Session cap reached:** Landed commits reach `max_session_commits` or cumulative net non-test/non-move LOC reaches `max_session_loc`.
  3. **Quota pressure:** `swarm{action:"models"}` reports low remaining quota on the active Gemini 4 families.
  4. **Work exhausted:** Zero eligible audit zones and zero eligible auto-fixable tracks remain.
- **Model Arbitrage Rule (`model-triage`):** Active Gemini 4 lineup ONLY (`Hydrogen`, `Neon`, `Carbon`). Never use `Argon`, `Gemini 3.8 Flash`, `Claude`, `GPT-OSS`, `Model="flash"`, or `Orchestrator` inside `/swarm`. Upgrade a stalled worker in-place via `swarm{action:"model"}` rather than `kill` + `add`.
- **Atomic Commits for Morning Review:** Land one clean, revertible conventional commit per fixer track (`fix(<zone>): ...`, `refactor(<zone>): ...`, `test(<zone>): ...`) so the user can `git cherry-pick` or `git revert` individual commits in minutes.

---

## 3. Forbidden Autonomous Changes

Fixers are **strictly forbidden** from autonomously making any of the following changes overnight (the `auditor` may *report* them in `backlog.json` with `"safe_for_auto_fix": false`, and the read-only `architect` may draft decision RFCs under `code-health/rfcs/`):

1. **DocType schema & migration edits:** Any `*.json` under `ism-frappe/ism/ism/doctype/`, `ism-frappe/ism/patches.txt`, or `ism-frappe/ism/patches/*`.
2. **Routing & public contract breaks:** Changing `ism-frappe/ism/hooks.py` hooks/routes, renaming/removing `@frappe.whitelist` dotted method paths, or altering public REST/MCP/CLI parameter names or response schemas.
3. **Destructive git or data operations:** Rewriting git history (`rebase`, `reset --hard` on shared branches, `push --force`), deleting production fixtures, or modifying `.env` / secrets / `.agents/hooks.json`.
4. **Agent OS hygiene invariant:** `ism-agent-os/rules/AGENTS.md` MUST remain strictly `< 24,000` bytes and `ism-agent-os/tests/test_skills_validation.py` MUST pass.
5. **Silently altering specs to make tests pass:** If a newly written test exposes a real production bug, keep the bug documented (add/update an issue in `backlog.json` with exact repro) rather than weakening assertions or changing production behavior without explicit spec evidence.

---

## 4. Scoped Rules for Deep Tracks (`extended` & `overnight`)

### A. File-Size Reduction (`track_kind: "refactor"`, `category: "file-size"`)
Overnight modularization is high-risk if unconstrained. A fixer may split an oversized **Python (`.py`)** file **only** when all four conditions hold *(Frappe Desk `.js` doctype scripts require `hooks.py`/bundle changes and remain manual)*:
1. **Max 1 file split per night** (scheduled on Wave 3 of `overnight`, `<= 800` move-only changed LOC).
2. **Existing passing test coverage** for the target zone (verified green *before* refactoring, or tests added first in a separate prior wave/run).
3. **Strict backward compatibility:** Extract pure helpers/submodules into sibling modules while re-exporting all previously public symbols/functions from the original module so no Frappe dotted paths, imports, or callers break.
4. **Verification proof:** Both the zone's full test suite and import/syntax checks pass under a cross-family `verifier`.

### B. Test-Coverage Hardening (`track_kind: "test-hardening"`)
- **Zero production edits:** Worker may ONLY create/edit test files inside its assigned test paths.
- If a new regression test uncovers a genuine production defect, mark the test with a clear note or document the defect in the report for `backlog.json` rather than altering production behavior or weakening assertions.

### C. Static-Debt Sweeper (`track_kind: "static-sweeper"`)
- Targets up to **6** `static-scanner` `safe_for_auto_fix: true` items (`n-plus-one-candidate`, `unbounded-get-all`, `swallowed-exception`) in a single disjoint zone per wave (`gemini-4-hydrogen-medium`).
- Must NOT touch behaviorally ambiguous findings; propose `wontfix` with code proof on false positives for the cross-family verifier to confirm.

### D. Architect RFC Track (`kind: "rfc"`, Read-Only)
- Runs read-only (`workspace: "inherit"`, zero bench slots, zero application edits) on top 1–2 `open` Critical/High items marked `safe_for_auto_fix: false` that do not yet have `code-health/rfcs/<CH-ID>.md`.
- Posts the completed RFC markdown following `code-health/rfcs/_TEMPLATE.md` on the Swarm board; the `coordinator` (single writer) saves it to `code-health/rfcs/<CH-ID>.md`.

### E. Final Cross-Zone Integration Gate (`extended` & `overnight`)
- After the final wave lands on the active nightly branch, the coordinator runs `python code-health/scripts/health_cli.py integration-gate --zones <comma-list> --slot 4 --json` and executes the single batched Frappe suite (all touched zones' modules + `ism.tests.test_cross_track_seams`) and, if touched, the full `ism-agent-os` unittest suite.
- If the cross-zone gate uncovers a failure, identify the offending commit, `git revert --no-edit <sha>` on the nightly branch (preserving the diagnostic trace in `backlog.json` as `deferred`), and re-run the gate before closing out.

---

## 5. Static Scanner Thresholds & Deduplication

Default thresholds used by `code-health/scripts/health_cli.py`:

| Signal | Threshold / Rule | Default `safe_for_auto_fix` |
| :--- | :--- | :---: |
| `file-size-critical` | `> 1,500` LOC (non-test code) or `> 2,500` LOC (tests) | `false` *(requires scoped split plan)* |
| `file-size-warning` | `> 800` LOC (non-test code) | `false` |
| `long-function` | Python `def` / `async def` `> 120` LOC (non-test code) | `false` |
| `sqli-candidate` | `frappe.db.sql(...)` with f-string, `%`, or `.format()` | `false` *(security review first)* |
| `n-plus-one-candidate` | `frappe.get_doc` / `frappe.db.get_value` / `frappe.db.exists` inside `for`/`while` | `true` *(when small & test-covered)* |
| `unbounded-get-all` | `frappe.get_all` / `frappe.db.get_all` without `limit` or `limit_page_length` | `true` |
| `ignore-permissions-audit` | `ignore_permissions=True` in non-test code (audit signal) | `false` |
| `allow-guest-inventory` | `@frappe.whitelist(allow_guest=True)` in non-test code (audit signal) | `false` |
| `swallowed-exception` | Bare `except:` or `except Exception: pass` in non-test code | `true` |
| `todo-marker` | `FIXME`, `HACK`, or `XXX` in non-test code | `false` |

### Deduplication & Triage Memory
- Every issue receives a deterministic `fingerprint` (`sha1` prefix of `rule + normalized_file + symbol`).
- Re-running `health_cli.py scan --seed` **never** duplicates or re-opens an item whose `fingerprint` already exists with status `fixed`, `wontfix`, `deferred`, `in_progress`, or `awaiting_review`.
- To permanently silence a false positive or intentional pattern (e.g., a legitimate public booking endpoint with `allow_guest=True`), mark it `wontfix` with a short rationale via `health_cli.py update-issue`.
