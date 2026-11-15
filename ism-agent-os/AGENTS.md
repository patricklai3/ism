# ISM Agent OS

This repository provides the skills, CLI, direct Frappe REST client, and ingestion tools to operate a Frappe + ISM automotive repair shop through any AI agent platform.

See [Core Operational Rules](rules/AGENTS.md) for required safety invariants and status values.

## Repository Layout

- `rules/AGENTS.md`: Mandatory shop invariants, status values, and CLI rules.
- `skills/`: Role-based Standard Operating Procedures (`ism-service-advisor`, `ism-shop-dispatcher`, `ism-parts-specialist`, `ism-floor-technician`, `ism-shop-executive`).
- `tools/cli.py`: Zero-dependency CLI (`python -m tools.cli` / `ism-agent-os`) exposing all curated shop workflows (quotes, scheduling, vehicle intelligence, check-in inspections, parts/alternates/Tesla ingestion, floor control, checkout) and generic Frappe REST commands.
- `tools/client.py`: Programmatic Python client (`IsmAgentClient`) talking directly to Frappe REST (`/api/method/*` and `/api/resource/*`).
- `openwebui_tools.py`: Open WebUI `Tools` wrapper backed by `IsmAgentClient`.
- `ingestion/`: Normalizers and parsers for supplier catalogs and Tesla service manuals.
- `scripts/`: Live verification (`verify_connection.py`) and end-to-end shop walkthrough (`demo_shop_flow.py`).
