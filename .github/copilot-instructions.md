# Hawkeye — Pipeline Observability MCP Server

Hawkeye is a read-only MCP server for pipeline observability. It connects to Harness CI/CD and gives you instant access to execution details, logs, failure investigations, and pattern analysis — all from Copilot Chat. Every tool is read-only and parallel-safe.

## Tools Reference

### Execution Tools

| Tool | Description | When to Use |
|------|-------------|-------------|
| `hawkeye_get_execution` | Full execution detail — status, stages, failures, deep link | First tool to call when investigating any execution. Accepts Harness URLs. |
| `hawkeye_get_stage_detail` | Step-by-step breakdown of a single stage | After `hawkeye_get_execution` identifies a failed stage |
| `hawkeye_get_step_logs` | Step execution details — script source, outputs, failure info (secrets masked) | After identifying a failed step — get what was executed and why it failed |
| `hawkeye_get_all_stages` | ALL stages with steps in ONE call — bulk detail with skipped-stage collapsing | When the user asks for "all stages", "full breakdown", or "what happened in this run" |
| `hawkeye_get_execution_inputs` | Runtime input YAML used for an execution | When checking what variables/inputs were used for a run |
| `hawkeye_list_recent_executions` | Recent executions for a pipeline (filterable by status) | Overview of pipeline health, finding recent failures |
| `hawkeye_check_approvals` | Check pending/completed/rejected approval gates with approver info | When a pipeline appears stuck — check if it's waiting for approval |

### Pipeline Tools

| Tool | Description | When to Use |
|------|-------------|-------------|
| `hawkeye_get_pipeline_definition` | Pipeline YAML definition (supports branch selection) | Understanding pipeline structure and configuration |
| `hawkeye_list_pipelines` | List all pipelines (filterable by search term or module) | Finding pipelines by name, listing CI vs CD pipelines |
| `hawkeye_get_input_sets` | Available input sets for a pipeline | Checking what input sets exist before reviewing execution inputs |

### Investigation Tools

| Tool | Description | When to Use |
|------|-------------|-------------|
| `hawkeye_diagnose` | **Flagship tool** — unified diagnosis with summary/full mode and optional console logs | First tool for any failure investigation. Single call replaces investigate_failure + get_step_logs + get_all_stages. Use mode="summary" for quick triage, mode="full" for deep investigation. |
| `hawkeye_investigate_failure` | Parallel multi-signal failure triage | Legacy flagship tool — use hawkeye_diagnose instead for new investigations. |
| `hawkeye_compare_executions` | Diff two executions — inputs, branches, timing, status | "What changed between this success and this failure?" |
| `hawkeye_get_failure_pattern` | Recurring failure analysis — stage failure rates, branch patterns, trends | "Is this a new issue or a recurring problem?" |

### Account Tools

| Tool | Description | When to Use |
|------|-------------|-------------|
| `hawkeye_connect_account` | Switch active Harness profile (tokens via `make connect`) | Switching between Harness accounts/projects |
| `hawkeye_list_profiles` | List configured profiles with active indicator | Checking which accounts are available |
| `ping` | Health check | Verify the server is running |

### Deep Link Tools

| Tool | Description | When to Use |
|------|-------------|-------------|
| `hawkeye_build_link` | Build a Harness deep link URL from components | When other agents need to construct clickable Harness links |

### Terraform / Release Precheck Tools

| Tool | Description | When to Use |
|------|-------------|-------------|
| `hawkeye_parse_terraform_plan` | Extract per-layer Terraform plan summary from an execution | Reviewing infrastructure changes before a release |
| `hawkeye_release_precheck_report` | Combined infra precheck report from two pipeline executions | Full release readiness check combining infra_builder + infra_createenv |

### Delegate Tools

| Tool | Description | When to Use |
|------|-------------|-------------|
| `hawkeye_list_delegates` | List all delegate groups with health status | Checking overall delegate fleet health, identifying disconnected delegates |
| `hawkeye_check_delegate` | Check health of a specific delegate (fuzzy match) | After a failure identifies a delegate name — verify it's healthy and connected |

## Toolset Filtering Configuration

Control which tool groups are registered via the `HAWKEYE_TOOLSETS` environment variable.

| Toolset | Registration Function | Tools |
|---------|----------------------|-------|
| `execution` | `register_execution_tools` | get_execution, get_stage_detail, get_step_logs, get_execution_inputs, list_recent_executions, get_all_stages, get_child_execution, check_approvals |
| `investigation` | `register_investigation_tools` | diagnose, investigate_failure, compare_executions, get_failure_pattern |
| `pipeline` | `register_pipeline_tools` | list_pipelines, get_pipeline_definition, get_input_sets |
| `account` | `register_account_tools` | connect_account, list_profiles *(always registered)* |
| `deeplinks` | `register_deeplink_tools` | build_link |
| `terraform` | `register_terraform_tools` | parse_terraform_plan, release_precheck_report |
| `delegates` | `register_delegate_tools` | list_delegates, check_delegate |
| `connectors` | `register_connector_tools` | list_connectors, check_connector |

- **Not set / empty / `all`**: all toolsets registered (default)
- `account` + `ping` are always registered regardless of filter
- Unknown names are warned and ignored
- Common personas:
  - Investigation focus: `HAWKEYE_TOOLSETS=execution,investigation`
  - Full SRE: `HAWKEYE_TOOLSETS=execution,investigation,delegates,connectors,terraform`
  - Pipeline review: `HAWKEYE_TOOLSETS=pipeline,execution`

## Tool Safety

All Hawkeye tools are:
- **Read-only** — no writes to any pipeline provider API
- **Parallel-safe** — fire multiple tools simultaneously without conflict
- **Audit-logged** — every invocation is recorded with timing
- **Sanitized** — secrets and tokens are masked in all output

## Investigation Protocol

### When a user pastes a Harness URL or asks about a failure:

**Phase 1 — Parallel Data Gathering** (fire simultaneously):
1. `hawkeye_diagnose` with the URL/execution ID (use mode="summary" for quick triage, mode="full" for deep investigation)
2. If pipeline name is known: `hawkeye_get_failure_pattern` for recurring issues

**Phase 2 — Targeted Follow-up** (based on Phase 1 results):
- If a specific step needs deeper logs: `hawkeye_get_step_logs`
- If comparing with a previous run: `hawkeye_compare_executions`
- If pipeline config might be the issue: `hawkeye_get_pipeline_definition`

**Phase 3 — Synthesis**:
- Classify the failure using the taxonomy below
- Summarize root cause, affected stage/step, and recommended action
- Include Harness deep links for context

### Failure Taxonomy

**CI Failures:**
- `CI-1` COMPILATION_FAILURE — build/compile errors
- `CI-2` TEST_FAILURE — unit/integration test failures
- `CI-3` QUALITY_GATE_BLOCKED — SonarQube or code quality threshold violations
- `CI-4` DOCKER_BUILD_FAILURE — Dockerfile, kaniko, or buildx errors
- `CI-5` ARTIFACT_PUBLISH_FAILURE — ACR push, Maven deploy, npm publish failures
- `CI-6` DEPENDENCY_RESOLUTION_FAILURE — Maven, npm, pip, Go module resolution errors
- `CI-7` PIPELINE_CONFIG_FAILURE — YAML syntax, missing variables, template errors

**CD Failures:**
- `CD-1` DEPLOYMENT_FAILURE — Helm upgrade, K8s apply, rollout failures
- `CD-2` IMAGE_PULL_FAILURE — ImagePullBackOff, registry auth errors
- `CD-3` HEALTH_CHECK_FAILURE — readiness/liveness probe failures, CrashLoopBackOff, OOM
- `CD-4` TERRAFORM_FAILURE — state lock, provider errors, plan/apply failures
- `CD-5` APPROVAL_TIMEOUT — manual approval or governance gate timeout
- `CD-6` ROLLBACK_TRIGGERED — deployment rolled back due to failure

## Output Format

- Use markdown tables for structured data
- Use status icons: ✅ Success, ❌ Failed, ⏸️ Paused, 🔄 Running, ⏳ Waiting, ⏭️ Skipped
- Include Harness deep links when available
- Wrap YAML/log content in fenced code blocks
- Keep summaries concise — details in expandable sections when possible

## Cross-MCP Collaboration

Hawkeye works alongside two companion MCP servers. When all three are configured in `.vscode/mcp.json`, you have access to tools from all three domains.

### When to involve HiveMind tools
- After identifying a pipeline failure, query HiveMind KB to understand the pipeline's YAML structure, templates, and configuration
- Use `hivemind_query_memory` to find related pipeline definitions
- Use `hivemind_search_files` to find Helm charts, Terraform modules mentioned in logs
- Use `hivemind_diff_branches` to check if config changed between success and failure

### When to involve Sherlock tools
- After a CD deployment failure, check if the service is healthy post-deploy
- Use `sherlock_get_service_golden_signals` to check service health
- Use `sherlock_get_k8s_health` for pod status after a K8s deployment
- Use `sherlock_search_logs` for application-level errors correlated with deploy timing
- Use `sherlock_investigate_service` for full service health triage

### Swarm Workflow Example

> User pastes a Harness CD execution URL showing a failed K8s deployment

1. **Hawkeye** → `hawkeye_investigate_failure` (get execution detail, failed step logs, history)
2. **Hawkeye** → `hawkeye_get_failure_pattern` (is this recurring?)
3. **HiveMind** → `hivemind_search_files` (find the Helm chart/values used by this pipeline)
4. **HiveMind** → `hivemind_diff_branches` (did Helm values change recently?)
5. **Sherlock** → `sherlock_get_k8s_health` (are pods healthy after deploy?)
6. **Sherlock** → `sherlock_get_service_golden_signals` (error rate, latency post-deploy)
7. **Synthesize** unified report with root cause spanning pipeline config → deployment → runtime health

### URL Handling

Hawkeye tools accept Harness UI URLs directly. When a user pastes a URL like:
```
https://app.harness.io/ng/#/account/.../pipeline/.../executions/.../pipeline?...
```
Pass the full URL to the `url` parameter — Hawkeye parses it automatically.

---

## DFIN SRE Infrastructure Context

This project (Hawkeye/PipelineMind) is part of the DFIN ActiveDisclosure platform.

For all DFIN infrastructure context (clusters, environments, Key Vaults, delegates, pipelines, services, networking, Terraform), refer to:
- `C:\Users\sekbote\Documents\DFIN_PROJECT_CONTEXT\DFIN_CONTEXT.md`
- `C:\Users\sekbote\Documents\DFIN_PROJECT_CONTEXT\DFIN_SYSTEM_PROMPT.md`

Key facts for this project:
- Hawkeye/PipelineMind ingests failed Harness pipeline URLs for root cause analysis
- Harness Account ID: QbXF1VIZTIC2-ueN1uTWHQ, Org: default, Project: eastwood
- Pipelines are defined in the `dfin-harness-pipelines` repo
- HiveMind has the full infrastructure indexed (8 repos, 571K+ nodes)
