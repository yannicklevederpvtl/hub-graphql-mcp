import pytest

from entities import REDACTED, properties
from health import combine_statuses, extract_alerts, normalize, summarize_backends


@pytest.mark.parametrize(
    "props,expected",
    [
        ({"LastOperationState": "succeeded"}, "healthy"),
        ({"lastOperation.state": "failed"}, "failed"),
        ({"last_operation_state": "in progress"}, "in_progress"),
        ({"lastOperation": {"state": "succeeded"}}, "healthy"),
        ({}, "unknown"),
    ],
)
def test_last_operation_states(props, expected):
    assert normalize(props, None)["status"] == expected


def test_alerts_drive_degraded_and_failed():
    ok = {"LastOperationState": "succeeded"}
    warning = [{"name": "MySQL: System Ephemeral Disk Full", "severity": "warning",
                "state": "firing"}]
    assert normalize(ok, warning)["status"] == "degraded"
    down = [{"name": "MySQL: HA Cluster Ready failure", "severity": "warning", "state": "firing"}]
    assert normalize(ok, down)["status"] == "failed"
    critical = [{"name": "Disk", "severity": "CRITICAL", "state": "active"}]
    assert normalize(ok, critical)["status"] == "failed"
    resolved = [{"name": "MySQL: Server Not available", "severity": "critical",
                 "state": "resolved"}]
    assert normalize(ok, resolved)["status"] == "healthy"


def test_broken_state_is_not_mistaken_for_ok():
    alerts = [{"name": "Valkey persistence failure", "severity": "warning", "state": "broken"}]
    assert normalize({"LastOperationState": "succeeded"}, alerts)["status"] == "degraded"


def test_extract_alerts_from_unknown_shape():
    insights = {"insights": {"alerts": [
        {"title": "MySQL: HA Cluster Size dropped < 1", "severity": "critical",
         "status": "firing"},
        {"unrelated": True},
    ], "observations": [{"name": "note"}]}}
    assert extract_alerts(insights) == [
        {"name": "MySQL: HA Cluster Size dropped < 1", "severity": "critical", "state": "firing"}
    ]


def test_combine_statuses_ignores_unknown():
    assert combine_statuses("unknown", "healthy") == "healthy"
    assert combine_statuses("unknown", "degraded", "healthy") == "degraded"
    assert combine_statuses("unknown") == "unknown"


def test_summarize_switchboard_backends():
    backends = [
        {"name": "mysql-0", "host": "10.0.0.1", "port": 3306, "healthy": True, "active": True,
         "currentSessionCount": 12, "trafficEnabled": True},
        {"name": "mysql-1", "host": "10.0.0.2", "port": 3306, "healthy": True, "active": False},
        {"name": "mysql-2", "host": "10.0.0.3", "port": 3306, "healthy": False, "active": False},
    ]
    summary = summarize_backends(backends)
    assert summary["status"] == "degraded"
    assert summary["primary"] == "mysql-0"
    assert summary["healthy_nodes"] == 2
    assert summarize_backends({"not": "a list"}) is None


def test_properties_redacts_secret_like_keys():
    entity = {"properties": [{"name": "GUID", "value": "g"},
                             {"name": "adminPassword", "value": "p"},
                             {"name": "service_api_key", "value": "k"}]}
    assert properties(entity) == {"GUID": "g", "adminPassword": REDACTED,
                                  "service_api_key": REDACTED}
