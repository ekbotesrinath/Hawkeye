# Hawkeye

A read-only MCP server for pipeline observability. Connects to Harness CI/CD and gives you instant access to execution details, logs, failure investigations, and pattern analysis — all from Copilot Chat.

## Quick Start

```bash
# 1. Clone and enter repo
git clone <repo-url> && cd hawkeye

# 2. Set up virtual environment and install dependencies
make setup

# 3. Configure your Harness connection
python scripts/setup.py
# or: make connect

# 4. Start the MCP server
make server

# 5. Open VS Code Copilot Chat and start asking questions
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Copilot Chat                    │
│          (VS Code / Claude / CLI)                │
└──────────────────────┬──────────────────────────┘
                       │ MCP (stdio)
┌──────────────────────▼──────────────────────────┐
│              Hawkeye MCP Server                  │
│                  main.py                         │
├─────────────┬──────────────┬────────────────────┤
│  tools/     │   core/      │   config/          │
│  executions │   models     │   settings         │
│  pipelines  │   sanitizer  │   (profiles.json)  │
│  investigate│   audit      │                    │
│  account    │   cache      │                    │
│  delegates  │   fuzzy      │                    │
├─────────────┴──────┬───────┴────────────────────┤
│              providers/                          │
│           base.py (abstract)                     │
│        harness/client.py (impl)                  │
│        harness/parser.py                         │
│        harness/url_patterns.py                   │
└──────────────────────┬──────────────────────────┘
                       │ HTTPS (read-only)
              ┌────────▼────────┐
              │   Harness API   │
              └─────────────────┘
```

## Tools

| # | Tool | Category | Description |
|---|------|----------|-------------|
| 1 | `hawkeye_get_execution` | Execution | Full execution detail — status, stages, failures, deep link |
| 2 | `hawkeye_get_stage_detail` | Execution | Step-by-step breakdown of a single stage |
| 3 | `hawkeye_get_step_logs` | Execution | Raw log output from a specific step (secrets masked) |
| 4 | `hawkeye_get_execution_inputs` | Execution | Runtime input YAML used for an execution |
| 5 | `hawkeye_list_recent_executions` | Execution | Recent executions (filterable by status) |
| 6 | `hawkeye_get_all_stages` | Execution | ALL stages with steps in one call (skipped-stage collapsing) |
| 7 | `hawkeye_get_child_execution` | Execution | Follow child pipeline executions |
| 8 | `hawkeye_check_approvals` | Execution | Check pending/completed/rejected approval gates |
| 9 | `hawkeye_get_pipeline_definition` | Pipeline | Pipeline YAML definition (supports branch selection) |
| 10 | `hawkeye_list_pipelines` | Pipeline | List pipelines (filterable by search or module) |
| 11 | `hawkeye_get_input_sets` | Pipeline | Available input sets for a pipeline |
| 12 | `hawkeye_list_templates` | Template | List templates (filterable by search, type, or scope) |
| 13 | `hawkeye_get_template` | Template | Template YAML definition (fuzzy match, version selection, project/org/account scope) |
| 14 | `hawkeye_diagnose` | Investigation | **Flagship** — unified diagnosis (summary/full mode, optional console logs) |
| 15 | `hawkeye_investigate_failure` | Investigation | Parallel multi-signal failure triage |
| 16 | `hawkeye_compare_executions` | Investigation | Diff two executions — inputs, branches, timing |
| 17 | `hawkeye_get_failure_pattern` | Investigation | Recurring failure analysis and trends |
| 18 | `hawkeye_connect_account` | Account | Switch active Harness profile |
| 19 | `hawkeye_list_profiles` | Account | List configured profiles |
| 20 | `hawkeye_list_delegates` | Delegate | List all delegate groups with health status |
| 21 | `hawkeye_check_delegate` | Delegate | Check health of a specific delegate (fuzzy match) |
| 22 | `hawkeye_list_connectors` | Connector | List connectors with connectivity status |
| 23 | `hawkeye_check_connector` | Connector | Test connectivity of a specific connector |
| 24 | `hawkeye_build_link` | Deeplinks | Build Harness UI deep links |
| 25 | `hawkeye_parse_terraform_plan` | Terraform | Parse Terraform plan output |
| 26 | `hawkeye_release_precheck_report` | Terraform | Release precheck report |
| 27 | `hawkeye_list_secrets` | Secrets | List secret **metadata** — type, secret manager, last modified (never values) |
| 28 | `hawkeye_check_secret` | Secrets | Resolve a `<+secrets.getValue(...)>` ref to its metadata (fuzzy match, scope prefix) |
| 29 | `hawkeye_list_environments` | Environment | List environments (Production/PreProduction) |
| 30 | `hawkeye_get_environment` | Environment | Environment YAML — variables and overrides (fuzzy match) |
| 31 | `hawkeye_list_infrastructures` | Environment | List infrastructure definitions for an environment |
| 32 | `hawkeye_get_infrastructure` | Environment | Infrastructure YAML — connectorRef, cluster/namespace (fuzzy match) |
| 33 | `hawkeye_list_services` | Environment | List services in the project |
| 34 | `hawkeye_get_service` | Environment | Service YAML — manifests, artifacts, variables (fuzzy match) |
| 35 | `ping` | Utility | Health check |
| 36 | `hawkeye_watch_execution` | Investigation | Poll a running execution until it completes, then auto-diagnose failures |
| 37 | `hawkeye_get_flaky_steps` | Investigation | Step-level flakiness rates across recent runs (flaky vs consistently broken) |
| 38 | `hawkeye_health_overview` | Health | One-call triage: delegates + connectors + 24h failures + pending approvals |
| 39 | `hawkeye_deployment_map` | Health | Service × environment matrix of latest successful CD deploys |

All tools are **read-only**, **parallel-safe**, **audit-logged**, and **secret-sanitized**.

## Swarm Agents

Hawkeye includes 5 Copilot Chat agents for coordinated failure investigation:

| Agent | Role | Primary Tools |
|-------|------|---------------|
| `hawkeye-lead` | Investigation coordinator — routes queries, synthesizes reports | `hawkeye_diagnose`, `hawkeye_investigate_failure`, `hawkeye_get_failure_pattern` |
| `hawkeye-execution` | Execution data specialist — retrieves and interprets run details | `hawkeye_get_execution`, `hawkeye_get_stage_detail`, `hawkeye_get_execution_inputs`, `hawkeye_check_approvals` |
| `hawkeye-logs` | Log analyst — parses raw logs and classifies failures | `hawkeye_get_step_logs`, `hawkeye_get_stage_detail` |
| `hawkeye-patterns` | Pattern detector — recurring failures, flaky pipelines, trends | `hawkeye_get_failure_pattern`, `hawkeye_compare_executions` |
| `hawkeye-artifacts` | Build & deployment tracker — pipeline definitions and inputs | `hawkeye_get_pipeline_definition`, `hawkeye_get_input_sets`, `hawkeye_list_pipelines` |

## Swarm Integration

Hawkeye works alongside two companion MCP servers for full-stack pipeline observability:

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Hawkeye    │    │   HiveMind   │    │   Sherlock   │
│  (Pipeline)  │    │  (Infra KB)  │    │ (Observability)│
│              │    │              │    │              │
│ Executions   │    │ Terraform    │    │ New Relic    │
│ Logs         │    │ Helm charts  │    │ APM / Logs   │
│ Patterns     │    │ Pipelines    │    │ K8s health   │
│ Definitions  │    │ Secrets      │    │ Golden signals│
└──────┬───────┘    └──────┬───────┘    └──────┬───────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │
                    Copilot Chat
                  (unified interface)
```

**Example workflow** — user pastes a failed K8s deployment URL:
1. **Hawkeye** investigates the failure and checks for recurring patterns
2. **HiveMind** finds the Helm chart and checks for recent config changes
3. **Sherlock** checks pod health and service golden signals post-deploy
4. **Copilot** synthesizes a unified report with root cause across all three domains

## Project Structure

```
hawkeye/
├── main.py                 # MCP server entry point
├── Makefile                # Developer commands
├── pyproject.toml          # Dependencies and tool config
├── CLAUDE.md               # Claude Code instructions
├── scripts/
│   └── setup.py            # Interactive first-time setup wizard
├── config/
│   ├── __init__.py
│   ├── settings.py         # Multi-tenant profile management
│   └── status.py           # `make status` — profiles + connectivity check
├── core/
│   ├── models.py           # Pydantic data models + shared status sets
│   ├── sanitizer.py        # Secret masking + injection scrubbing
│   ├── audit.py            # Tool invocation logging
│   ├── cache.py            # TTL response cache (wired into the client)
│   ├── formatters.py       # Durations, icons, tables, log truncation
│   └── fuzzy.py            # Fuzzy name resolution
├── providers/
│   ├── base.py             # Abstract provider interface + ProviderError
│   └── harness/
│       ├── client.py       # Harness API client (retry, cache, pagination)
│       ├── parser.py       # API response parser
│       ├── graph_search.py # Step matching + console-log extraction
│       └── url_patterns.py # Harness URL parser
├── tools/
│   ├── _helpers.py         # Shared tool utilities (audit/sanitize wrapper)
│   ├── _gather.py          # Shared investigation orchestration
│   ├── executions.py       # 8 execution tools
│   ├── pipelines.py        # 3 pipeline tools
│   ├── investigate.py      # 6 investigation tools
│   ├── health.py           # 2 health tools (overview + deployment map)
│   ├── templates.py        # 2 template tools
│   ├── secrets.py          # 2 secret metadata tools
│   ├── environments.py     # 6 environment/service tools
│   ├── delegates.py        # 2 delegate tools
│   ├── connectors.py       # 2 connector tools
│   ├── deeplinks.py        # 1 deep-link builder
│   ├── terraform.py        # 2 Terraform report tools
│   └── account.py          # 2 account tools
├── tests/
│   ├── conftest.py         # Shared fixtures
│   ├── fixtures/           # JSON API response fixtures
│   └── test_*.py           # 800+ tests
└── .github/
    ├── workflows/ci.yml    # Lint + typecheck + tests + pip-audit
    ├── copilot-instructions.md
    └── agents/
        ├── hawkeye-lead.agent.md
        ├── hawkeye-execution.agent.md
        ├── hawkeye-logs.agent.md
        ├── hawkeye-patterns.agent.md
        └── hawkeye-artifacts.agent.md
```

## Security Model

- **Read-only at transport** — PUT/DELETE/PATCH are blocked outright, and
  POST is restricted to an explicit allowlist of read-semantics endpoints
  (list/summary/test queries). A future code path cannot accidentally
  trigger, approve, or abort a pipeline.
- **Tokens stay in the OS keyring** — never in config files or logs. Signed
  log-download links are fetched with a bare HTTP client so the Harness API
  key is never sent to blob storage hosts.
- **Output sanitization** — every tool response is scrubbed for prompt
  injection (replaced with a visible marker) and masked for secrets
  (GitHub/AWS/Slack tokens, JWTs, PEM keys, connection strings, and
  high-entropy strings — while git SHAs, image digests, and URLs pass
  through untouched).
- **Bounded resource use** — log downloads capped at 50 MB, decompression
  at 20 MB, at most 8 concurrent API requests, retry with jitter honoring
  `Retry-After`, and clamped list limits.
- **MCP annotations** — every tool advertises `readOnlyHint=true` so MCP
  clients can display and authorize accordingly (enforced by a test).

## Development

```bash
make setup      # Create venv and install dependencies
make test       # Run full test suite (pytest -v --tb=short)
make test-cov   # Run with coverage (fail under 80%)
make lint       # Ruff check + mypy
make server     # Start MCP server
make connect    # Configure Harness credentials
make status     # Show active profile and connectivity
```

## Toolset Filtering

As the tool surface grows, you can limit which tool groups are registered using
the `HAWKEYE_TOOLSETS` environment variable.  This keeps the LLM's context
window focused on the tools relevant to your workflow.

| Toolset | Tools Included |
|---------|---------------|
| `execution` | get_execution, get_stage_detail, get_step_logs, get_execution_inputs, list_recent_executions, get_all_stages, get_child_execution, check_approvals |
| `investigation` | diagnose, investigate_failure, compare_executions, get_failure_pattern, watch_execution, get_flaky_steps |
| `pipeline` | list_pipelines, get_pipeline_definition, get_input_sets |
| `templates` | list_templates, get_template |
| `account` | connect_account, list_profiles *(always registered)* |
| `deeplinks` | build_link |
| `terraform` | parse_terraform_plan, release_precheck_report |
| `delegates` | list_delegates, check_delegate |
| `connectors` | list_connectors, check_connector |
| `secrets` | list_secrets, check_secret |
| `environments` | list_environments, get_environment, list_infrastructures, get_infrastructure, list_services, get_service |
| `health` | health_overview, deployment_map |

**Behavior:**
- **Not set / empty / `all`** — all toolsets are registered (default, backward-compatible).
- **Comma-separated list** — only those toolsets are registered.
- `account` and `ping` are **always** registered regardless of filter.
- Unknown names are logged as warnings and ignored.

**Persona examples:**

```bash
# Investigation focus (SRE triaging failures)
HAWKEYE_TOOLSETS=execution,investigation

# Full SRE toolkit
HAWKEYE_TOOLSETS=execution,investigation,delegates,connectors,terraform

# Pipeline reviewer
HAWKEYE_TOOLSETS=pipeline,execution
```

To set in your VS Code MCP config (`.vscode/mcp.json`):

```json
{
  "servers": {
    "hawkeye": {
      "command": "python",
      "args": ["main.py"],
      "cwd": "/path/to/hawkeye",
      "env": {
        "HAWKEYE_TOOLSETS": "execution,investigation,delegates"
      }
    }
  }
}
```

## Contributing

1. All tools must be read-only — no writes to provider APIs
2. All tool outputs must pass through `core/sanitizer.py`
3. All tool invocations must be audit-logged via `tools/_helpers.py:run_tool()`
4. Tests must pass before committing: `make test`
5. Follow the existing code patterns in `tools/*.py`
