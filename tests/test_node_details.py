"""Tests for providers/harness/parser.py — extract_node_details and helpers."""

from __future__ import annotations

from providers.harness.parser import (
    _EXPORT_RE,
    _SECRET_KEY_RE,
    _extract_config,
    _extract_script,
    _format_duration_from_ts,
    _mask_secret_value,
    _mask_secrets_in_dict,
    _mask_secrets_in_script,
    _status_icon_str,
    extract_node_details,
)

_NOW_MS = 1_700_000_000_000  # fixed reference timestamp


# ------------------------------------------------------------------
# _mask_secret_value
# ------------------------------------------------------------------


class TestMaskSecretValue:
    def test_short_value_fully_masked(self) -> None:
        assert _mask_secret_value("abc") == "****"

    def test_empty_string(self) -> None:
        assert _mask_secret_value("") == "****"

    def test_exactly_four_chars(self) -> None:
        assert _mask_secret_value("abcd") == "****"

    def test_long_value_shows_prefix(self) -> None:
        assert _mask_secret_value("ghp_abc123xyz") == "ghp_****"


# ------------------------------------------------------------------
# _mask_secrets_in_dict
# ------------------------------------------------------------------


class TestMaskSecretsInDict:
    def test_secret_key_masked(self) -> None:
        result = _mask_secrets_in_dict({"api_key": "secret_value_123", "name": "deploy"})
        assert result["api_key"] == "secr****"
        assert result["name"] == "deploy"

    def test_token_key_masked(self) -> None:
        result = _mask_secrets_in_dict({"AUTH_TOKEN": "my_long_token"})
        assert result["AUTH_TOKEN"] == "my_l****"

    def test_password_key_masked(self) -> None:
        result = _mask_secrets_in_dict({"db_password": "p@ssw0rd!"})
        assert result["db_password"] == "p@ss****"

    def test_non_secret_key_unmasked(self) -> None:
        result = _mask_secrets_in_dict({"namespace": "production"})
        assert result["namespace"] == "production"

    def test_empty_dict(self) -> None:
        assert _mask_secrets_in_dict({}) == {}


# ------------------------------------------------------------------
# _mask_secrets_in_script
# ------------------------------------------------------------------


class TestMaskSecretsInScript:
    def test_export_secret_masked(self) -> None:
        script = "export API_TOKEN=ghp_abc123xyz\necho hello"
        result = _mask_secrets_in_script(script)
        assert "ghp_****" in result
        assert "echo hello" in result

    def test_export_non_secret_unmasked(self) -> None:
        script = "export NAMESPACE=production"
        result = _mask_secrets_in_script(script)
        assert "production" in result

    def test_no_exports(self) -> None:
        script = "echo 'hello world'\nls -la"
        result = _mask_secrets_in_script(script)
        assert result == script


# ------------------------------------------------------------------
# _extract_script
# ------------------------------------------------------------------


class TestExtractScript:
    def test_shell_script_source_spec(self) -> None:
        params = {"spec": {"source": {"spec": {"script": "echo deploy"}}}}
        assert _extract_script(params, "ShellScript") == "echo deploy"

    def test_run_command(self) -> None:
        params = {"spec": {"command": "npm test"}}
        assert _extract_script(params, "Run") == "npm test"

    def test_direct_script(self) -> None:
        params = {"spec": {"script": "make build"}}
        assert _extract_script(params, "Run") == "make build"

    def test_no_script(self) -> None:
        params = {"spec": {"connectorRef": "docker"}}
        assert _extract_script(params, "K8sRolling") is None

    def test_none_params(self) -> None:
        assert _extract_script(None, "Run") is None

    def test_non_dict_spec(self) -> None:
        params = {"spec": "not_a_dict"}
        assert _extract_script(params, "Run") is None


# ------------------------------------------------------------------
# _extract_config
# ------------------------------------------------------------------


class TestExtractConfig:
    def test_k8s_deploy_config(self) -> None:
        params = {"spec": {"releaseName": "myapp", "namespace": "prod", "connectorRef": "k8s"}}
        config = _extract_config(params, "K8sRollingDeploy")
        assert config["releaseName"] == "myapp"
        assert config["namespace"] == "prod"
        assert config["connectorRef"] == "k8s"

    def test_approval_config(self) -> None:
        params = {"spec": {"approvalMessage": "Please approve", "approvers": "admin"}}
        config = _extract_config(params, "HarnessApproval")
        assert "approvalMessage" in config

    def test_http_config(self) -> None:
        params = {"spec": {"url": "https://api.example.com", "method": "GET"}}
        config = _extract_config(params, "Http")
        assert config["url"] == "https://api.example.com"
        assert config["method"] == "GET"

    def test_generic_config(self) -> None:
        params = {"spec": {"image": "node:18", "count": 3, "enabled": True}}
        config = _extract_config(params, "CustomStep")
        assert config["image"] == "node:18"
        assert config["count"] == "3"

    def test_none_params(self) -> None:
        assert _extract_config(None, "Run") == {}


# ------------------------------------------------------------------
# _format_duration_from_ts
# ------------------------------------------------------------------


class TestFormatDurationFromTs:
    def test_seconds_only(self) -> None:
        assert _format_duration_from_ts(_NOW_MS, _NOW_MS + 14_000) == "14s"

    def test_minutes_and_seconds(self) -> None:
        assert _format_duration_from_ts(_NOW_MS, _NOW_MS + 130_000) == "2m 10s"

    def test_hours(self) -> None:
        result = _format_duration_from_ts(_NOW_MS, _NOW_MS + 3_922_000)
        assert result == "1h 5m 22s"

    def test_none_start(self) -> None:
        assert _format_duration_from_ts(None, _NOW_MS) == "—"

    def test_negative_duration(self) -> None:
        assert _format_duration_from_ts(_NOW_MS + 1000, _NOW_MS) == "—"


# ------------------------------------------------------------------
# _status_icon_str
# ------------------------------------------------------------------


class TestStatusIconStr:
    def test_success(self) -> None:
        assert _status_icon_str("Success") == "✅"

    def test_failed(self) -> None:
        assert _status_icon_str("Failed") == "❌"

    def test_errored(self) -> None:
        assert _status_icon_str("Errored") == "❌"

    def test_running(self) -> None:
        assert _status_icon_str("Running") == "🔄"

    def test_skipped(self) -> None:
        assert _status_icon_str("Skipped") == "⏭️"

    def test_unknown(self) -> None:
        assert _status_icon_str("SomeWeirdStatus") == "❓"

    def test_empty(self) -> None:
        assert _status_icon_str("") == "❓"


# ------------------------------------------------------------------
# extract_node_details — main function
# ------------------------------------------------------------------


class TestExtractNodeDetails:
    def test_empty_node(self) -> None:
        assert extract_node_details({}) == "No step data available."

    def test_basic_fields(self) -> None:
        node = {
            "name": "Build",
            "stepType": "Run",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 60_000,
        }
        result = extract_node_details(node)
        assert "Build" in result
        assert "Run" in result
        assert "✅" in result
        assert "1m 0s" in result

    def test_shell_script_with_script(self) -> None:
        node = {
            "name": "Deploy",
            "stepType": "ShellScript",
            "status": "Failed",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 30_000,
            "stepParameters": {
                "spec": {"source": {"spec": {"script": "kubectl apply -f deploy.yaml"}}}
            },
        }
        result = extract_node_details(node)
        assert "kubectl apply" in result
        assert "Executed:" in result

    def test_failure_info(self) -> None:
        node = {
            "name": "Build",
            "stepType": "Run",
            "status": "Failed",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 10_000,
            "failureInfo": {
                "message": "Exit code 1",
                "failureTypeList": ["APPLICATION_FAILURE"],
            },
        }
        result = extract_node_details(node)
        assert "Exit code 1" in result
        assert "APPLICATION_FAILURE" in result

    def test_delegate_info(self) -> None:
        node = {
            "name": "Deploy",
            "stepType": "K8sRollingDeploy",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 5_000,
            "delegateInfoList": [{"name": "delegate-abc"}],
        }
        result = extract_node_details(node)
        assert "delegate-abc" in result

    def test_output_variables_masked(self) -> None:
        node = {
            "name": "GetToken",
            "stepType": "ShellScript",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 5_000,
            "outcomes": {
                "output": {
                    "outputVariables": {
                        "API_TOKEN": "ghp_secret123456",
                        "version": "1.2.3",
                    }
                }
            },
        }
        result = extract_node_details(node)
        assert "ghp_****" in result
        assert "1.2.3" in result

    def test_environment_variables_masked(self) -> None:
        node = {
            "name": "Deploy",
            "stepType": "Run",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 5_000,
            "stepParameters": {
                "spec": {
                    "environmentVariables": {
                        "DB_PASSWORD": "super_secret_pw",
                        "APP_PORT": "8080",
                    }
                }
            },
        }
        result = extract_node_details(node)
        assert "supe****" in result
        assert "8080" in result

    def test_subgraph_enriches_delegate(self) -> None:
        node = {
            "name": "Deploy",
            "stepType": "K8sRolling",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 5_000,
        }
        subgraph = {"delegateInfoList": [{"name": "enriched-delegate"}]}
        result = extract_node_details(node, subgraph)
        assert "enriched-delegate" in result

    def test_subgraph_step_parameters(self) -> None:
        node = {
            "name": "Build",
            "stepType": "Run",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 5_000,
        }
        subgraph = {
            "nodeMap": {
                "node1": {
                    "stepParameters": {
                        "spec": {"command": "make build"}
                    }
                }
            }
        }
        result = extract_node_details(node, subgraph)
        assert "make build" in result

    def test_non_script_step_shows_config(self) -> None:
        node = {
            "name": "Deploy Prod",
            "stepType": "K8sRollingDeploy",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 5_000,
            "stepParameters": {
                "spec": {
                    "releaseName": "myapp-prod",
                    "namespace": "production",
                }
            },
        }
        result = extract_node_details(node)
        assert "myapp-prod" in result or "Config:" in result

    def test_script_masking_in_node(self) -> None:
        node = {
            "name": "Setup",
            "stepType": "ShellScript",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 5_000,
            "stepParameters": {
                "spec": {
                    "source": {
                        "spec": {
                            "script": "export API_TOKEN=ghp_verysecretvalue\necho done"
                        }
                    }
                }
            },
        }
        result = extract_node_details(node)
        assert "ghp_****" in result
        assert "echo done" in result

    def test_failure_response_messages(self) -> None:
        node = {
            "name": "Approval",
            "stepType": "HarnessApproval",
            "status": "Failed",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 60_000,
            "failureInfo": {
                "message": "Approval timed out",
                "failureTypeList": ["TIMEOUT_FAILURE"],
                "responseMessages": [
                    {"message": "No approver responded within 30 minutes"}
                ],
            },
        }
        result = extract_node_details(node)
        assert "Approval timed out" in result
        assert "TIMEOUT_FAILURE" in result

    def test_subgraph_outcomes_override_node(self) -> None:
        """Subgraph outcomes should be preferred when richer."""
        node = {
            "name": "GetVersion",
            "stepType": "ShellScript",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 5_000,
            "outcomes": {"output": {"outputVariables": {"v": "1"}}},
        }
        subgraph = {
            "outcomes": {
                "output": {
                    "outputVariables": {"v": "1", "build_num": "42"}
                }
            }
        }
        result = extract_node_details(node, subgraph)
        assert "42" in result

    def test_lite_engine_task_expired_shows_infra_analysis(self) -> None:
        """Expired liteEngineTask should surface CI infrastructure analysis."""
        node = {
            "name": "liteEngineTask",
            "stepType": "liteEngineTask",
            "status": "Expired",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 600_000,
            "failureInfo": {
                "message": "Please Check the timeout configuration on the step to extend the duration of the step",
                "failureTypeList": ["EXPIRED"],
                "responseMessages": [
                    {"message": "Please Check the timeout configuration on the step to extend the duration of the step"}
                ],
            },
            "stepDetails": {
                "initStepV2DelegateTaskInfo": {
                    "taskID": "task-123",
                    "taskName": "INITIALIZATION_PHASE",
                }
            },
            "stepParameters": {
                "identifier": "liteEngineTask",
                "name": "liteEngineTask",
                "timeout": "10m",
                "type": "liteEngineTask",
                "spec": {},
            },
        }
        result = extract_node_details(node)
        assert "CI Infrastructure Issue" in result
        assert "INITIALIZATION_PHASE" in result
        assert "Timeout Config: 10m" in result
        assert "K8s node scheduling failure" in result

    def test_lite_engine_task_success_no_infra_analysis(self) -> None:
        """Successful liteEngineTask should NOT show infra issue warning."""
        node = {
            "name": "liteEngineTask",
            "stepType": "liteEngineTask",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 30_000,
            "stepDetails": {
                "initStepV2DelegateTaskInfo": {
                    "taskID": "task-123",
                    "taskName": "INITIALIZATION_PHASE",
                }
            },
            "stepParameters": {
                "timeout": "10m",
                "spec": {},
            },
        }
        result = extract_node_details(node)
        assert "CI Infrastructure Issue" not in result
        assert "INITIALIZATION_PHASE" in result

    def test_log_key_surfaced(self) -> None:
        """Log keys from executableResponses should be surfaced."""
        node = {
            "name": "liteEngineTask",
            "stepType": "liteEngineTask",
            "status": "Expired",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 600_000,
            "executableResponses": [
                {
                    "async": {
                        "callbackIds": ["cb1"],
                        "logKeys": ["account/pipeline/123/stage/liteEngineTask"],
                    }
                }
            ],
        }
        result = extract_node_details(node)
        assert "Log Key: account/pipeline/123/stage/liteEngineTask" in result

    def test_k8s_rolling_deploy_failed_shows_cd_infra_analysis(self) -> None:
        """Failed K8sRollingDeploy should surface CD infrastructure analysis."""
        node = {
            "name": "Rollout Deployment",
            "stepType": "K8sRollingDeploy",
            "status": "Failed",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 27_000,
            "failureInfo": {
                "message": "Steady state check failed with error:\ndeployment export-orchestration-service exceeded its progress deadline",
                "failureTypeList": ["APPLICATION_ERROR"],
                "responseMessages": [
                    {"message": "Resources failed to reach steady state."},
                    {"message": "Steady state check failed with error:\ndeployment export-orchestration-service exceeded its progress deadline"},
                ],
            },
        }
        result = extract_node_details(node)
        assert "CD Infrastructure Issue" in result
        assert "progress deadline" in result.lower() or "Diagnosis:" in result

    def test_k8s_deploy_success_no_infra_analysis(self) -> None:
        """Successful K8sRollingDeploy should NOT show infra issue warning."""
        node = {
            "name": "Rollout Deployment",
            "stepType": "K8sRollingDeploy",
            "status": "Success",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 30_000,
        }
        result = extract_node_details(node)
        assert "CD Infrastructure Issue" not in result

    def test_k8s_deploy_expired_shows_infra_analysis(self) -> None:
        """Expired K8sRollingDeploy should surface CD infrastructure analysis."""
        node = {
            "name": "Rollout Deployment",
            "stepType": "K8sRollingDeploy",
            "status": "Expired",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 600_000,
            "failureInfo": {
                "message": "Please Check the timeout configuration on the step to extend the duration of the step",
                "failureTypeList": ["EXPIRED"],
            },
        }
        result = extract_node_details(node)
        assert "CD Infrastructure Issue" in result

    def test_k8s_deploy_with_step_details_events(self) -> None:
        """K8s deployment step details with cluster events should be surfaced."""
        node = {
            "name": "Rollout Deployment",
            "stepType": "K8sRollingDeploy",
            "status": "Failed",
            "startTs": _NOW_MS,
            "endTs": _NOW_MS + 27_000,
            "failureInfo": {
                "message": "Steady state check failed",
                "failureTypeList": ["APPLICATION_ERROR"],
            },
            "stepDetails": {
                "k8sTaskDetails": {
                    "k8sClusterEventLogs": "0/13 nodes available: 2 Insufficient cpu, 6 Insufficient memory",
                },
            },
        }
        result = extract_node_details(node)
        assert "Cluster Events" in result
        assert "Insufficient cpu" in result

    def test_expired_status_icon(self) -> None:
        assert _status_icon_str("Expired") == "⏰"


# ------------------------------------------------------------------
# Regex patterns
# ------------------------------------------------------------------


class TestSecretPatterns:
    def test_secret_key_matches(self) -> None:
        for key in ["api_key", "SECRET_VAL", "Password", "AUTH_TOKEN", "bearer_token", "sas_key"]:
            assert _SECRET_KEY_RE.search(key), f"{key} should match secret pattern"

    def test_non_secret_keys(self) -> None:
        for key in ["namespace", "image", "count", "port"]:
            assert not _SECRET_KEY_RE.search(key), f"{key} should not match secret pattern"

    def test_export_pattern(self) -> None:
        m = _EXPORT_RE.match("export MY_VAR=some_value")
        assert m is not None
        assert m.group(2) == "MY_VAR"
        assert m.group(4) == "some_value"
