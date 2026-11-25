# RFC — {{CH_ID}}: {{TITLE}}

- **Issue ID:** `{{CH_ID}}`
- **Severity / Category:** `{{SEVERITY}}` / `{{CATEGORY}}`
- **Zone:** `{{ZONE}}`
- **Authoring Run:** `{{RUN_ID}}` *(Read-Only `architect` track)*
- **Status:** `awaiting-human-decision`

---

## 1. Problem & Verified Code Evidence

*(Exact file paths, line numbers, symbol names, and concrete failure / exploit scenario verified directly against source.)*

## 2. Callsite & Blast-Radius Inventory

| File & Line | Symbol / Caller | Role / Auth Context | Impact of Change |
| :--- | :--- | :--- | :--- |
| — | — | — | — |

*(Note any DocType `.json`, `hooks.py`, frontend SPA, MCP tool, or external webhook contract touchpoints.)*

## 3. Design Options & Trade-Offs

### Option A — *(Recommended)*
- **Approach:**
- **Pros:**
- **Risks / Migration Cost:**

### Option B — *(Alternative)*
- **Approach:**
- **Pros:**
- **Risks / Migration Cost:**

## 4. Recommended Implementation & Verification Plan

1. *(Concrete ordered file changes)*
2. *(Exact regression test cases to add)*
3. *(Exact `test-in-slot.ps1` / `unittest` verification command)*
4. **Auto-fix readiness after approval:** *(Yes — flip `safe_for_auto_fix: true` once Option A/B is chosen | No — requires schema migration / manual rollout)*

---

## 5. Morning Decision Prompt

- **Decision needed:** *(1–2 crisp sentences asking the human to approve Option A vs Option B, plus the exact `health_cli.py update-issue` command to unlock the next nightly run if approved.)*
