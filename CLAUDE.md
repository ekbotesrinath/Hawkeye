# Hawkeye — Pipeline Observability MCP Server

## Quick Reference
- Python 3.12, `.venv`
- `make test` — run full test suite
- `make lint` — ruff + mypy over the whole repo (must pass)
- `make audit` — pip-audit dependency scan
- `make server` — start MCP server
- `make connect` — configure Harness credentials
- `make status` — show profiles + live connectivity check
- All tools are read-only
- Tests AND lint must pass before committing (CI enforces both)

## Architecture
```
tools/*.py → tools/_gather.py → providers/base.py → providers/harness/*.py → Harness API
```

- `tools/` — MCP tool definitions (execution, pipeline, investigation, account)
- `tools/_gather.py` — shared investigation orchestration (failed-step details,
  CI infra enrichment, child pipeline following) — the ONLY home for that
  logic; never re-implement it inside a tool
- `core/` — models, sanitizer, audit logger, cache, fuzzy matching
- `core/formatters.py` — single home for duration/icon/truncation helpers
- `providers/` — abstract base (+ `ProviderError`) + Harness implementation
- `providers/harness/graph_search.py` — step matching + console-log extraction
- `config/` — multi-tenant profile management (stored in `~/.hawkeye/`)

## Tools (39 total)

### Execution (8)
- `hawkeye_get_execution` — full execution detail from ID or URL
- `hawkeye_get_stage_detail` — step-by-step stage breakdown
- `hawkeye_get_step_logs` — raw log output (last 200 lines, secrets masked)
- `hawkeye_get_execution_inputs` — runtime input YAML
- `hawkeye_list_recent_executions` — recent runs with status filter
- `hawkeye_get_all_stages` — all stages with steps in one call
- `hawkeye_get_child_execution` — follow child pipeline executions
- `hawkeye_check_approvals` — check pending/completed/rejected approval gates

### Pipeline (3)
- `hawkeye_get_pipeline_definition` — pipeline YAML (supports branch)
- `hawkeye_list_pipelines` — list/search pipelines by name or module
- `hawkeye_get_input_sets` — available input sets for a pipeline

### Template (2)
- `hawkeye_get_template` — template YAML by identifier (fuzzy match, optional version, project/org/account scope)
- `hawkeye_list_templates` — list/search templates by name or type (Step, Stage, Pipeline, …)

### Investigation (6)
- `hawkeye_diagnose` — unified diagnosis tool (summary/full mode, optional console logs)
- `hawkeye_investigate_failure` — parallel multi-signal failure triage
- `hawkeye_compare_executions` — diff two executions
- `hawkeye_get_failure_pattern` — recurring failure analysis
- `hawkeye_watch_execution` — poll a running execution until terminal, then auto-diagnose
- `hawkeye_get_flaky_steps` — step-level flakiness rates across recent runs (flaky vs consistently broken, branch-specific notes)

### Health (2)
- `hawkeye_health_overview` — one-call triage: disconnected delegates + failing connectors + last-24h failures + waiting-on-human executions + running
- `hawkeye_deployment_map` — service × environment matrix of latest successful CD deploys

### Account (2)
- `hawkeye_connect_account` — switch active profile
- `hawkeye_list_profiles` — list configured profiles

### Delegate (2)
- `hawkeye_list_delegates` — list all delegate groups with health status
- `hawkeye_check_delegate` — check health of a specific delegate (fuzzy match)

### Connector (2)
- `hawkeye_list_connectors` — list connectors with connectivity status
- `hawkeye_check_connector` — test connectivity of a specific connector

### Secrets (2) — metadata only, never values
- `hawkeye_list_secrets` — list secret metadata by search/type/scope
- `hawkeye_check_secret` — resolve a secret ref (e.g. `account.global_git_hub_pat` from `<+secrets.getValue(...)>`) to type, secret manager, and created/last-modified timestamps (fuzzy match)

### Environment (6)
- `hawkeye_list_environments` — list environments (Production/PreProduction)
- `hawkeye_get_environment` — environment YAML with variables/overrides (fuzzy match)
- `hawkeye_list_infrastructures` — infrastructure definitions for an environment
- `hawkeye_get_infrastructure` — infrastructure YAML (connectorRef, cluster/namespace)
- `hawkeye_list_services` — list services
- `hawkeye_get_service` — service YAML (manifests, artifacts, variables)

### Deeplinks (1)
- `hawkeye_build_link` — build Harness UI deep links

### Terraform (2)
- `hawkeye_parse_terraform_plan` — parse Terraform plan output
- `hawkeye_release_precheck_report` — release precheck report

### Utility (1)
- `ping` — health check

## Rules
- Never write to pipeline provider APIs — read-only is enforced at transport:
  PUT/DELETE/PATCH are blocked, and POST is restricted to an explicit
  allowlist of read-semantics endpoints (`_ALLOWED_POST_PATHS` in
  `providers/harness/client.py`). Adding a new POST endpoint means adding it
  to that allowlist deliberately.
- Never log or expose API tokens. The signed log-download link MUST be
  fetched with the bare (headerless) client — never the authenticated one.
- All tool outputs must be sanitized via `core/sanitizer.py`
- All tool invocations must be audit logged via `core/audit.py`
- Use `tools/_helpers.py:run_tool()` wrapper for every tool to ensure audit + sanitize
- Every `@mcp.tool()` must pass `annotations=READ_ONLY_ANNOTATIONS`
  (from `tools/_helpers.py`) — a test enforces this
- Clamp numeric tool params with `tools/_helpers.py:clamp_limit()`
- Fuzzy name resolution (`core/fuzzy.py`) resolves partial pipeline names
- Profiles and tokens stored in `~/.hawkeye/` via keyring
- Listings paginate (up to 5×100) and are cached per-client (60 s TTL);
  completed executions are cached 10 min — see `providers/harness/client.py`

## Configuration

### Toolset Filtering (`HAWKEYE_TOOLSETS`)
Comma-separated list of toolset names to register. Default (unset/empty): all.
- Toolsets: `execution`, `investigation`, `pipeline`, `templates`, `account`, `deeplinks`, `terraform`, `delegates`, `connectors`, `secrets`, `environments`, `health`
- Singular/plural spellings both accepted (e.g. `executions`, `secret`)
- `account` + `ping` are always registered
- Unknown names are warned and ignored
- Example: `HAWKEYE_TOOLSETS=execution,investigation` — only load execution + investigation + account + ping
