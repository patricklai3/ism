# Move Natural-Language Date/Time Normalization From Frappe Backend to Agent

## Problem
- `d:\ism\ism-frappe\ism\scheduling\time_utils.py` implements rigid rule-based NLP (`normalize_date` L59-106, `normalize_time` L109-160, `_WEEKDAY_MAP` L26-44, `_NAMED_TIMES` L46-51, wired into `to_timedelta` L163-191).
- `_NAMED_TIMES` silently guesses fixed times (`"morning"` = `09:00:00`, `"afternoon"` = `13:00:00`) instead of checking a time window or asking for clarification.
- Rule-based `if/elif` + regex in Python is brittle for open-ended phrasing, while the LLM agent already handles natural language natively.
- `d:\ism\ism-agent-os\rules\AGENTS.md` does not yet instruct the agent to resolve relative dates/times to ISO `YYYY-MM-DD` and 24h `HH:MM:SS` before invoking the CLI.

## Target Fix (After Speed Loop Stabilizes)
1. Update `d:\ism\ism-agent-os\rules\AGENTS.md` (and scheduling skills) so the agent always resolves natural-language dates/times (`today`, `tomorrow`, weekdays, `"morning"`, `"afternoon"`) into exact ISO `YYYY-MM-DD` and `HH:MM:SS` (or searches a slot window for vague windows like `"morning"`).
2. Remove `_NAMED_TIMES` and weekday/relative-date `if/elif` parsing from `ism/scheduling/time_utils.py` (optionally keep only strict ISO + simple `9am`/`2:30pm` 12h parsing as a lightweight fallback, or require strict ISO).
3. Update `ism/tests/test_scheduling_capacity.py` and `ism/tests/test_agent_facade.py` to match the tightened backend contract.
