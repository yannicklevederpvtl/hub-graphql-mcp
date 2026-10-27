"""Normalize Hub inventory data into healthy / degraded / failed / in_progress / unknown."""

from __future__ import annotations

import re
from typing import Any

from entities import prop

STATUS_ORDER = ("healthy", "unknown", "in_progress", "degraded", "failed")

LAST_OPERATION_STATE = re.compile(r"last.?operation.?state|lastoperation\.state", re.I)
FAILED_ALERT = re.compile(
    r"not available|instance is down|cluster ready failure|cluster status failure|"
    r"cluster size dropped|offline partitions|node disconnected",
    re.I,
)
CRITICAL_SEVERITY = re.compile(r"crit|fatal|emerg|\bsev[ _-]?1\b|\bp1\b", re.I)
INACTIVE_STATE = re.compile(
    r"\b(resolved|inactive|closed|cancell?ed|ok|normal|silenced)\b", re.I
)
ALERT_NAME_KEYS = ("name", "title", "alertName", "definitionName", "displayName", "summary")
ALERT_SEVERITY_KEYS = ("severity", "criticality", "level", "priority")
ALERT_STATE_KEYS = ("state", "status", "alertState")


def worst(*statuses: str) -> str:
    return max(statuses, key=STATUS_ORDER.index)


def combine_statuses(*statuses: str) -> str:
    """Worst known status; `unknown` only when no source knows better."""
    known = [s for s in statuses if s in STATUS_ORDER and s != "unknown"]
    return worst(*known) if known else "unknown"


def last_operation_state(props: dict[str, Any]) -> str | None:
    for key, value in props.items():
        if LAST_OPERATION_STATE.search(key) and value:
            return str(value)
    value = prop(props, "lastOperation", "last_operation")
    if isinstance(value, dict):
        return value.get("state")
    if isinstance(value, str):
        return value
    return None


def extract_alerts(insights: Any) -> list[dict[str, Any]]:
    """Collect alert-like records from an `insights` payload of unknown shape."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name = next((node[k] for k in ALERT_NAME_KEYS if isinstance(node.get(k), str)), None)
            severity = next((node[k] for k in ALERT_SEVERITY_KEYS if node.get(k)), None)
            state = next((node[k] for k in ALERT_STATE_KEYS if node.get(k)), None)
            if name and (severity is not None or state is not None):
                found.append({"name": name, "severity": severity, "state": state})
            for value in node.values():
                if isinstance(value, dict | list):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(insights)
    return found


def firing(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in alerts if not (a.get("state") and INACTIVE_STATE.search(str(a["state"])))]


def normalize(props: dict[str, Any], alerts: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Return {status, reasons, last_operation, firing_alerts} for one service instance."""
    reasons: list[str] = []
    status = "unknown"
    state = last_operation_state(props)
    lowered = (state or "").lower()
    if "fail" in lowered:
        status = "failed"
        reasons.append(f"last operation {state}")
    elif "progress" in lowered or "pending" in lowered:
        status = "in_progress"
        reasons.append(f"last operation {state}")
    elif "succeed" in lowered or "success" in lowered or "complete" in lowered:
        status = "healthy"

    active = firing(alerts or [])
    for alert in active:
        severe = FAILED_ALERT.search(alert["name"]) or (
            alert.get("severity") and CRITICAL_SEVERITY.search(str(alert["severity"]))
        )
        status = worst(status, "failed" if severe else "degraded")
        reasons.append(f"alert firing: {alert['name']}")

    if status == "unknown" and state is None:
        reasons.append("no last-operation state in Hub inventory")
    return {
        "status": status,
        "reasons": reasons,
        "last_operation": state,
        "firing_alerts": active,
        "alerts_source": "hub-insights" if alerts is not None else "not-configured",
    }


def bosh_vm_status(props: dict[str, Any]) -> str:
    """Health of one Hub `Tanzu.TAS.BoshVM` from its BOSH `State` and agent `Status`."""
    state = str(props.get("State") or "").lower()
    if props.get("Status") is False or state in {"stopped", "detached", "unresponsive",
                                                  "failing"}:
        return "failed"
    if state in {"starting", "stopping", "updating"}:
        return "in_progress"
    if state in {"started", "running"}:
        return "healthy"
    return "unknown"


def summarize_vms(vms: list[dict[str, Any]]) -> dict[str, Any]:
    """Cluster view: all VMs down is failed, some down is degraded (an HA cluster survives)."""
    statuses = [vm["status"] for vm in vms]
    failed = statuses.count("failed")
    if not vms:
        status = "unknown"
    elif failed == len(vms):
        status = "failed"
    elif failed:
        status = "degraded"
    else:
        status = combine_statuses(*statuses)
    return {
        "status": status,
        "node_count": len(vms),
        "healthy_nodes": statuses.count("healthy"),
        "nodes": vms,
    }


def summarize_backends(payload: Any) -> dict[str, Any] | None:
    """Summarize a MySQL proxy `/v0/backends` list; None when the payload has another shape."""
    if not isinstance(payload, list) or not payload or not all(
        isinstance(b, dict) and "healthy" in b for b in payload
    ):
        return None
    nodes = [
        {
            "name": b.get("name") or b.get("host"),
            "host": b.get("host"),
            "port": b.get("port"),
            "healthy": bool(b.get("healthy")),
            "role": "primary" if b.get("active") else "standby",
            "current_sessions": b.get("currentSessionCount"),
            "traffic_enabled": b.get("trafficEnabled"),
        }
        for b in payload
    ]
    healthy = sum(n["healthy"] for n in nodes)
    primaries = [n for n in nodes if n["role"] == "primary"]
    if healthy == 0:
        status = "failed"
    elif healthy < len(nodes) or len(primaries) != 1:
        status = "degraded"
    else:
        status = "healthy"
    return {
        "status": status,
        "node_count": len(nodes),
        "healthy_nodes": healthy,
        "primary": primaries[0]["name"] if len(primaries) == 1 else None,
        "nodes": nodes,
    }
