# ISM Agent OS

This repository provides the skills, MCP server, and ingestion tools to operate a Frappe + ISM automotive repair shop through any AI agent platform.

See [Core Operational Rules](rules/AGENTS.md) for required safety invariants and status values.

## Repository Layout

- `rules/AGENTS.md`: Mandatory shop invariants and authorization gates.
- `skills/`: Role-based Standard Operating Procedures (`ism-service-advisor`, `ism-shop-dispatcher`, `ism-parts-specialist`, `ism-floor-technician`, `ism-shop-executive`).
- `tools/ism_mcp_server.py`: Zero-dependency Stdio JSON-RPC 2.0 MCP server exposing 19 shop and Frappe tools.
- `tools/client.py`: Programmatic Python client (`IsmAgentClient`) with MCP and REST fallback.
- `ingestion/`: Normalizers and parsers for supplier catalogs and Tesla service manuals.
- `scripts/`: Live verification (`verify_connection.py`) and end-to-end shop walkthrough (`demo_shop_flow.py`).
