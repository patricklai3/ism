# ISM

Monorepo for **ISM** (automotive/EV service shop management built on **Frappe / ERPNext v16**), combining the Frappe backend & portals, AI agent tooling, local WSL2 bench scripts, and code-health automation.

## Repository Structure

| Directory | Purpose | Docs |
| :--- | :--- | :--- |
| `ism-frappe/` | Core Frappe / ERPNext v16 app (`ism`) — Repair Orders, Scheduling, Tech Workbench, Customer Portal, Inventory, Comms, and Agent APIs | [ism-frappe/README.md](ism-frappe/README.md) |
| `ism-agent-os/` | AI agent skills, operational rules, Python client (`IsmAgentClient`), and CLI (`ism-agent-os`) over Frappe REST | [ism-agent-os/README.md](ism-agent-os/README.md) |
| `scripts/` | Windows ↔ WSL2 bench sync, isolated parallel worktree test slots (`slot-1`–`slot-5`), trunk promotion, Cloudflare Tunnel, and subtree deploy scripts | [scripts/README.md](scripts/README.md) |
| `code-health/` | Stateful nightly Swarm code-health scanner, zone map, and issue ledger | [code-health/README.md](code-health/README.md) |
| `.github/workflows/` | GitHub Actions subtree sync (`sync-ism-frappe.yml`) to the Frappe Cloud deploy repo | [frappe-cloud-staging.md](ism-frappe/docs/frappe-cloud-staging.md) |

## Quick Start

### 1. Run `ism-frappe` Tests (Isolated Bench Slots)
Run migrations, tests, scripts, or dev previews from `main` or any Git worktree without touching `development.localhost` (`:8000`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test-in-slot.ps1 -Module ism.tests.test_estimation_math
powershell -ExecutionPolicy Bypass -File scripts\provision-bench-slots.ps1 -Status -History 10
```

### 2. Sync & Promote on `main`
From the primary checkout (`D:\ism` on `main`):

```powershell
# Sync ism-frappe to WSL2 bench, update golden DB snapshot, and run smoke tests
powershell -ExecutionPolicy Bypass -File scripts\promote-to-main.ps1

# Live-watch file sync to WSL2
powershell -ExecutionPolicy Bypass -File scripts\watch-sync.ps1
```

### 3. Connect `ism-agent-os`
Configure API credentials and verify connectivity against your Frappe site:

```powershell
cd ism-agent-os
Copy-Item .env.example .env
python -m tools.cli test
```

### 4. Push `ism-frappe` to Frappe Cloud Deploy Repo
`ism-frappe/` is synced via `git subtree split` so `pyproject.toml` and `ism/` sit at the deploy repo root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\push-deploy-subtree.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\push-deploy-subtree.ps1 -Branch staging
```
