"""
Built-in workflow action templates (BigID services-configuration PUTs).

Seeded into workflow_templates on startup. Keys match workflow step `notes` and
`action::…` aliases resolved by workflow_runner._resolve_action_template.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy.engine import Engine

SERVICES_WEB_STACK = ["orchestrator", "orchestrator2", "web"]
SERVICES_ORCH_PAIR = ["orchestrator", "orchestrator2"]


def _svc_cfg_put(
    config_id: str,
    service: str,
    value: str,
    *,
    services: List[str] | None = None,
    is_restart_required: bool = True,
    is_editable: bool = False,
    verify_config: bool = False,
) -> Dict[str, Any]:
    call: Dict[str, Any] = {
        "method": "PUT",
        "path": "/api/v1/services-configuration",
        "body": {
            "configuration": {
                "id": config_id,
                "name": config_id,
                "isEditable": is_editable,
                "isRestartRequired": is_restart_required,
                "markAsSensitiveKey": False,
                "service": service,
                "services": services or SERVICES_WEB_STACK,
                "type": "boolean",
                "value": value,
                "hasConflicts": False,
                "isGrouped": True,
            }
        },
    }
    if verify_config:
        call["verify_config"] = True
    return call


def enable_ds_lite_mode_calls() -> List[Dict[str, Any]]:
    """Enable DS_LITE_MODE_ENABLED on orchestrator, orchestrator2, and web."""
    calls: List[Dict[str, Any]] = []
    for svc in SERVICES_WEB_STACK:
        calls.append(
            _svc_cfg_put(
                "DS_LITE_MODE_ENABLED",
                svc,
                "true",
                is_restart_required=False,
                is_editable=True,
                verify_config=True,
            )
        )
    return calls


def disable_ols_feature_flags_calls() -> List[Dict[str, Any]]:
    """
    Disable OLS-related service flags (from OLS Disable.postman_collection.json).

    SCANS_QUEUE_ENABLED is intentionally omitted: setting it false blocks all scan starts
    and breaks workflow scan steps until services are restarted and the flag is re-enabled.
    """
    calls: List[Dict[str, Any]] = []
    for svc in SERVICES_WEB_STACK:
        calls.append(
            _svc_cfg_put(
                "OBJECT_LEVEL_PARTITIONING",
                svc,
                "false",
                is_restart_required=True,
                is_editable=False,
            )
        )
    for svc in SERVICES_WEB_STACK:
        calls.append(
            _svc_cfg_put(
                "OBJECT_LEVEL_ACTIONS_ENABLED",
                svc,
                "false",
                is_restart_required=True,
                is_editable=False,
            )
        )
    for svc in SERVICES_ORCH_PAIR:
        calls.append(
            _svc_cfg_put(
                "METADATA_PARTS_ENABLED",
                svc,
                "true",
                services=SERVICES_ORCH_PAIR,
                is_restart_required=True,
                is_editable=False,
            )
        )
    return calls


def enable_ols_feature_flags_calls() -> List[Dict[str, Any]]:
    """Re-enable OLS flags before the OLS scan block (inverse of disable_ols)."""
    calls: List[Dict[str, Any]] = []
    for svc in SERVICES_WEB_STACK:
        calls.append(
            _svc_cfg_put(
                "OBJECT_LEVEL_PARTITIONING",
                svc,
                "true",
                is_restart_required=True,
                is_editable=False,
            )
        )
    for svc in SERVICES_WEB_STACK:
        calls.append(
            _svc_cfg_put(
                "OBJECT_LEVEL_ACTIONS_ENABLED",
                svc,
                "true",
                is_restart_required=True,
                is_editable=False,
            )
        )
    for svc in SERVICES_ORCH_PAIR:
        calls.append(
            _svc_cfg_put(
                "METADATA_PARTS_ENABLED",
                svc,
                "true",
                services=SERVICES_ORCH_PAIR,
                is_restart_required=True,
                is_editable=False,
            )
        )
    for svc in SERVICES_ORCH_PAIR:
        calls.append(
            _svc_cfg_put(
                "SCANS_QUEUE_ENABLED",
                svc,
                "true",
                services=SERVICES_ORCH_PAIR,
                is_restart_required=True,
                is_editable=False,
            )
        )
    return calls


def workflow_action_template_seeds() -> Dict[str, List[Dict[str, Any]]]:
    ds_lite = enable_ds_lite_mode_calls()
    disable_ols = disable_ols_feature_flags_calls()
    enable_ols = enable_ols_feature_flags_calls()
    return {
        "ENABLE DS_LITE MODE": ds_lite,
        "action::ENABLE DS_LITE MODE": ds_lite,
        "action::enable_ds_lite": ds_lite,
        "DISABLE OLS FEATURE FLAGS": disable_ols,
        "action::DISABLE OLS FEATURE FLAGS": disable_ols,
        "action::disable_ols": disable_ols,
        "ENABLE OLS FEATURE FLAGS": enable_ols,
        "action::ENABLE OLS FEATURE FLAGS": enable_ols,
        "action::pre_ols": enable_ols,
        "action::enable_ols": enable_ols,
    }


def seed_workflow_action_templates(engine: Engine) -> None:
    """Upsert built-in action templates (idempotent)."""
    from v2_api.workflow_runner import upsert_template_value  # noqa: PLC0415

    for key, calls in workflow_action_template_seeds().items():
        upsert_template_value(engine, key, "action", json.dumps(calls))
