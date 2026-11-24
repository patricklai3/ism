# Code Health Operating Policy & Guardrails

Every nightly or recurring Swarm run MUST obey these guardrails. When a task conflicts with this policy, **stop, document the issue in `backlog.json` (or mark it `deferred`), and escalate to the morning dossier** rather than guessing or loosening safety rules.

---

## 1. Branch & Environment Isolation (Non-Negotiable)

1. **Never edit or commit on `main`.** The user creates and checks out a dedicated branch (e.g., `nightly/YYYY-MM-DD`) before starting a run. `health_cli.py preflight` fails if `HEAD` is on `main` or detached.
2. **Zero `development.localhost:8000` contact.** Never run `sync-to-wsl.ps1`, `watch-sync.*`, `promote-to-main.ps1`, or direct `wsl` / `docker exec` bench commands targeting primary `frappe-bench`.
3. **Isolated Bench Slots Only.** Every `ism-frappe` verification command runs strictly via:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\test-in-slot.ps1 -Slot <1..5> -Module <mod1,mod2>
   ```
   Do not run parallel `test-in-slot.ps1` calls inside the same worktree; batch modules comma-separated in one invocation.
4. **No remote pushes.** Never `git push` during an autonomous run.
5. **Single-Writer Ledger Ownership.** Only the `coordinator` modifies files under `code-health/`. Fixer and auditor workers report findings and commit SHAs on the Swarm board.

---

## 2. Quota Presets & Blast-Radius Caps

| Preset | Swarm Workers | Audit Zones / Run | Fix Tracks | Max Items / Fixer | Max Changed LOC / Fixer | Max Retries / Item |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **`eco`** | `monitor`, `auditor`, `fixer-a`, `verifier` | 1 | 1 | 1–2 | `<= 250` LOC | 2 |
| **`standard`** *(default)* | `monitor`, `auditor`, `fixer-a`, `fixer-b`, `verifier` | 1–2 | 2 (disjoint) | 1–2 | `<= 400` LOC | 2 |
| **`audit-only`** | `monitor`, `auditor-1`, `auditor-2` *(opt `auditor-3`)* | 2–3 | 0 | 0 | `0` LOC | 1 |

- **Retry Stop Rule:** If a fixer fails verification twice on the same item, stop working on it, mark it `deferred` with the failure trace in `backlog.json`, and do NOT burn extra quota looping.
- **Model Arbitrage Rule (`model-triage`):** Active Gemini 4 lineup ONLY (`Hydrogen`, `Neon`, `Carbon`). Never use `Argon`, `Gemini 3.8 Flash`, `Claude`, `GPT-OSS`, `Model="flash"`, or `Orchestrator` inside `/swarm`. Upgrade a stalled worker in-place via `swarm{action:"model"}` rather than `kill` + `add`.

---

## 3. Forbidden Autonomous Changes

Fixers are **strictly forbidden** from autonomously making any of the following changes overnight (the `auditor` may *report* them in `backlog.json` with `"safe_for_auto_fix": false` for human planning):

1. **DocType schema & migration edits:** Any `*.json` under `ism-frappe/ism/ism/doctype/`, `ism-frappe/ism/patches.txt`, or `ism-frappe/ism/patches/*`.
2. **Routing & public contract breaks:** Changing `ism-frappe/ism/hooks.py` hooks/routes, renaming/removing `@frappe.whitelist` dotted method paths, or altering public REST/MCP/CLI parameter names or response schemas.
3. **Destructive git or data operations:** Rewriting git history (`rebase`, `reset --hard` on shared branches, `push --force`), deleting production fixtures, or modifying `.env` / secrets / `.agents/hooks.json`.
4. **Agent OS hygiene invariant:** `ism-agent-os/rules/AGENTS.md` MUST remain strictly `< 24,000` bytes and `ism-agent-os/tests/test_skills_validation.py` MUST pass.
5. **Silently altering specs to make tests pass:** If a newly written test exposes a real production bug, keep the bug documented (add/update an issue in `backlog.json` with exact repro) rather than weakening assertions or changing production behavior without explicit spec evidence.

---

## 4. Scoped Rules for File-Size Reduction (`category: "file-size"`)

Overnight modularization is high-risk if unconstrained. A fixer may split an oversized file **only** when all four conditions hold:

1. **Max 1 file split per fixer per run.**
2. **Existing passing test coverage** for the target zone (verified green *before* refactoring, or tests added first in a separate prior run).
3. **Strict backward compatibility:** Extract pure helpers/submodules into sibling modules while re-exporting all previously public symbols/functions from the original module so no Frappe dotted paths, imports, or callers break.
4. **Verification proof:** Both the zone's test suite and import/syntax checks pass under the cross-family `verifier`.

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
