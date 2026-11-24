# Code Health Run Dossier — {{RUN_ID}}

- **Date:** {{DATE}}
- **Preset:** `{{PRESET}}`
- **Active Branch:** `{{BRANCH}}`
- **Base Commit:** `{{BASE_SHA}}`
- **Swarm Models Used:** *(fill in coordinator / auditor / fixers / verifier)*

---

## Executive Summary

| Metric | Value |
| :--- | :---: |
| Zones Audited | `0` (`{{AUDIT_ZONES}}`) |
| New Issues Discovered | `0` |
| Fix Tracks Executed | `0` (`{{FIX_ZONES}}`) |
| Commits Landed on Branch | `0` |
| Items Deferred / Blocked | `0` |

---

## Landed Changes Ready for Morning Review

| Worker | Zone | Item IDs | Commit SHA | Diff Stat | Verifier Verdict |
| :--- | :--- | :--- | :---: | :--- | :---: |
| *(none yet)* | — | — | — | — | — |

### Verification Proof
```text
(Paste exact verifier test commands and pass counts here)
```

---

## New Issues Discovered (`backlog.json`)

| ID | Severity | Category | Zone | File / Line | Auto-Fixable | Summary |
| :--- | :---: | :--- | :--- | :--- | :---: | :--- |
| *(none yet)* | — | — | — | — | — | — |

---

## Needs Human Decision / Deferred Items

- *(List any architectural tradeoffs, schema recommendations, real bugs uncovered by new tests, or items deferred after 2 retries.)*

---

## Morning Merge Commands

```powershell
git log --oneline main..{{BRANCH}}
git diff --stat main..{{BRANCH}}

# When satisfied:
git checkout main
git merge --no-ff {{BRANCH}}
powershell -ExecutionPolicy Bypass -File scripts\promote-to-main.ps1 -Fast
```
