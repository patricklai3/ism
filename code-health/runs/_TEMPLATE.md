# Code Health Run Dossier — {{RUN_ID}}

- **Date:** {{DATE}}
- **Preset:** `{{PRESET}}`
- **Active Branch:** `{{BRANCH}}`
- **Base Commit:** `{{BASE_SHA}}`
- **Waves Completed:** `1 / 1` *(stop reason: completed | circuit-breaker: <reason>)*
- **Swarm Models Used:** *(fill in coordinator / auditors / architect / fixers / verifiers)*

---

## Executive Summary

| Metric | Value |
| :--- | :---: |
| Waves Completed | `0` |
| Zones Audited | `0` (`{{AUDIT_ZONES}}`) |
| New Verified Semantic Issues Added | `0` |
| Static Signals Triaged (Cross-Verified) | `0` |
| Fix / Deep Tracks Executed | `0` (`{{FIX_ZONES}}`) |
| Commits Landed on Branch | `0` |
| Architect RFCs Generated | `0` |
| Items Deferred / Blocked | `0` |
| Final Cross-Zone Integration Gate | `N/A` *(PASS / FAIL)* |

---

## Landed Changes Ready for Morning Review

### Tier 1 — Low-Risk / Auto-Merge Ready *(tests, static sweepers, small isolated fixes)*
| Wave | Worker | Track Kind | Zone | Item IDs | Commit SHA | Diff Stat | Verifier Verdict |
| :---: | :--- | :--- | :--- | :--- | :---: | :--- | :---: |
| — | *(none yet)* | — | — | — | — | — | — |

### Tier 2 — Careful Review *(security, payments/billing, concurrency, `§4` file-size refactors)*
| Wave | Worker | Track Kind | Zone | Item IDs | Commit SHA | Diff Stat | Verifier Verdict |
| :---: | :--- | :--- | :--- | :--- | :---: | :--- | :---: |
| — | *(none yet)* | — | — | — | — | — | — |

### Verification Proof & Final Cross-Zone Integration Gate
```text
(Paste per-wave verifier commands + final `integration-gate` cross-zone test output here)
```

---

## Architect RFCs Generated (`code-health/rfcs/`)

| Item ID | Severity | Zone | RFC Path | Morning Decision Prompt |
| :---: | :---: | :--- | :--- | :--- |
| *(none yet)* | — | — | — | — |

---

## New Issues Discovered (`backlog.json`)

| ID | Severity | Category | Zone | File / Line | Auto-Fixable | Summary |
| :--- | :---: | :--- | :--- | :--- | :---: | :--- |
| *(none yet)* | — | — | — | — | — | — |

---

## Needs Human Decision / Deferred Items

- *(List any architectural tradeoffs, schema recommendations, real bugs uncovered by new tests, or items deferred after 2 retries / integration-gate reverts.)*

---

## Morning Merge Commands

```powershell
git log --oneline main..{{BRANCH}}
git diff --stat main..{{BRANCH}}

# When satisfied (merge entire nightly branch):
git checkout main
git merge --no-ff {{BRANCH}}
powershell -ExecutionPolicy Bypass -File scripts\promote-to-main.ps1 -Fast

# Or selectively revert any single commit before merging:
# git revert --no-edit <commit_sha>
```
