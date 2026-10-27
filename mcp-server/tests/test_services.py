import pytest
from conftest import FakeHub, entity, make_settings

from services import HubService

FOUNDATION = entity("tas-east", "Tanzu.TAS.Foundation")
ORG = entity("payments", "Tanzu.TAS.Organization", foundation=[FOUNDATION])
OTHER_ORG = entity("retail", "Tanzu.TAS.Organization", foundation=[FOUNDATION])
SPACE = entity("prod", "Tanzu.TAS.Space", organization=[ORG])
OTHER_SPACE = entity("prod", "Tanzu.TAS.Space", organization=[OTHER_ORG])
MYSQL = entity("p.mysql", "Tanzu.TAS.ServiceOffering")
REDIS = entity("p.redis", "Tanzu.TAS.ServiceOffering")
HA_PLAN = entity("db-ha", "Tanzu.TAS.ServicePlan")
GUID = "4f1d9a52-1c55-4a7e-9a39-2b4b0c6f0c11"


def instance(name, org_space=SPACE, offering=MYSQL, state="succeeded", **extra):
    props = {"GUID": GUID if name == "orders-db" else f"guid-{name}",
             "LastOperationState": state, "adminPassword": "hunter2"}
    return entity(name, props=props, space=[org_space], offering=[offering], plan=[HA_PLAN],
                  **extra)


def vm(index, state="started", agent=True):
    return entity(f"mysql-{index}", "Tanzu.TAS.BoshVM",
                  {"JobName": "mysql", "Index": index, "State": state, "Status": agent,
                   "AZ": "az1", "IPs": [f"10.0.0.{index}"]})


DEPLOYMENT = entity("service-instance_orders", "Tanzu.TAS.Deployment",
                    vms=[vm(0), vm(1), vm(2, state="stopped")])
ORDERS = instance(
    "orders-db",
    relatedOut=[DEPLOYMENT, entity("orders-api", "Tanzu.TAS.Application")],
    relatedIn=[],
    deployment=[DEPLOYMENT],
    insights=[{"alerts": [{"name": "MySQL: HA Cluster Ready failure", "severity": "critical",
                           "state": "firing"}]}],
)
CACHE = instance("cache", org_space=OTHER_SPACE, offering=REDIS)
BROKEN = instance("legacy-db", state="failed")


def service(entities, **settings_overrides):
    settings = make_settings(**settings_overrides)
    return HubService(FakeHub(entities), settings)


async def test_list_spaces_maps_org_and_foundation():
    result = await service([SPACE]).list_spaces()
    assert result["items"] == [{
        "name": "prod", "entity_id": SPACE["entityId"], "guid": None,
        "org": "payments", "foundation": "tas-east",
    }]
    assert result["source"] == "tanzu-hub"


async def test_list_spaces_org_filter_is_server_side():
    svc = service([SPACE])
    await svc.list_spaces(org="payments")
    assert "entitiesTraversalFilter" in svc.hub.documents[-1]
    assert '"payments"' in svc.hub.documents[-1]


async def test_list_service_instances_filters_client_side_and_says_so():
    result = await service([ORDERS, CACHE]).list_service_instances(org="payments",
                                                                   offering="p.mysql")
    assert [i["name"] for i in result["items"]] == ["orders-db"]
    assert result["filtered_client_side"] == ["org", "offering"]
    item = result["items"][0]
    assert (item["org"], item["space"], item["foundation"], item["plan"]) == (
        "payments", "prod", "tas-east", "db-ha")


async def test_services_status_sorts_worst_first_and_uses_alerts():
    svc = service([CACHE, ORDERS, BROKEN], insights_selection="insights { alerts { name } }")
    result = await svc.list_services_status()
    assert [(i["name"], i["status"]) for i in result["items"]] == [
        ("legacy-db", "failed"), ("orders-db", "failed"), ("cache", "healthy")]
    assert result["summary"] == {"failed": 2, "healthy": 1}
    assert result["alerts_source"] == "hub-insights"


async def test_services_status_without_insights_relies_on_last_operation():
    result = await service([ORDERS]).list_services_status()
    assert result["items"][0]["status"] == "healthy"
    assert result["alerts_source"] == "not-configured"


async def test_only_unhealthy():
    result = await service([CACHE, BROKEN]).list_services_status(only_unhealthy=True)
    assert [i["name"] for i in result["items"]] == ["legacy-db"]


@pytest.mark.parametrize(
    "identifier,needle",
    [
        ("vrn/provider:TAS/x", 'entityId: ["vrn/provider:TAS/x"]'),
        (GUID, f'values: ["{GUID}"]'),
        ("orders-db", '"entityName", operator: EQ, values: ["orders-db"]'),
    ],
)
async def test_identifier_routing(identifier, needle):
    svc = service([ORDERS])
    await svc.get_service_instance(identifier)
    assert needle in svc.hub.documents[-1]


async def test_get_service_instance_detail_redacts_and_classifies():
    result = await service([ORDERS]).get_service_instance(GUID)
    assert result["properties"]["adminPassword"] == "[REDACTED]"
    assert result["bound_apps"] == ["orders-api"]
    assert result["offering"]["name"] == "p.mysql"
    assert "Tanzu.TAS.Deployment" in result["related"]


async def test_user_provided_instance_falls_back_to_offering_property():
    ups = entity("creds", props={"ServiceOfferingName": "user-provided"}, space=[SPACE])
    result = await service([ups]).list_service_instances()
    assert result["items"][0]["offering"] == "user-provided"


async def test_ambiguous_name_returns_candidates():
    result = await service([ORDERS, CACHE]).get_service_instance("db")
    assert "candidates" in result
    assert len(result["candidates"]) == 2


async def test_not_found():
    result = await service([]).get_service_instance(GUID)
    assert result["error"].startswith("no service instance found")


async def test_mysql_status_without_deep_provider():
    svc = service([ORDERS], insights_selection="insights { alerts { name } }")
    result = await svc.get_mysql_service_instance_status(GUID)
    assert result["status"] == "failed"
    assert "Tanzu.TAS.BoshVM" in svc.hub.documents[-1]
    assert result["bosh_deployment"] == "service-instance_orders"
    assert result["topology"]["available"] is False
    assert "warning" not in result


async def test_mysql_status_rolls_up_bosh_vms():
    result = await service([ORDERS]).get_mysql_service_instance_status(GUID)
    bosh = result["bosh_nodes"]
    assert [(n["name"], n["status"]) for n in bosh["nodes"]] == [
        ("mysql/0", "healthy"), ("mysql/1", "healthy"), ("mysql/2", "failed")]
    assert (bosh["status"], bosh["healthy_nodes"]) == ("degraded", 2)
    assert result["status"] == "degraded"


async def test_mysql_status_all_vms_down_is_failed():
    down = entity("dep", "Tanzu.TAS.Deployment", vms=[vm(0, agent=False)])
    orders = instance("orders-db", deployment=[down])
    result = await service([orders]).get_mysql_service_instance_status(GUID)
    assert result["bosh_nodes"]["status"] == "failed"
    assert result["status"] == "failed"


async def test_mysql_status_merges_deep_topology():
    class Deep:
        async def fetch(self, guid):
            assert guid == GUID
            return {"available": True, "status": "degraded", "primary": "mysql-0"}

    settings = make_settings(mysql_deep_status=True,
                             mysql_deep_url_template="https://x.test/{guid}")
    svc = HubService(FakeHub([ORDERS]), settings, Deep())
    result = await svc.get_mysql_service_instance_status(GUID)
    assert result["topology"]["primary"] == "mysql-0"
    assert result["status"] == "degraded"


async def test_mysql_status_warns_for_non_mysql_offering():
    result = await service([CACHE]).get_mysql_service_instance_status("cache")
    assert "does not look like MySQL" in result["warning"]


def binding(subject, scope, role, subject_type="USER", **user):
    scope_type = {"Tanzu.TAS.Space": "CLOUD_FOUNDRY_SPACE",
                  "Tanzu.TAS.Organization": "CLOUD_FOUNDRY_ORGANIZATION"}.get(
        scope["entityType"], "CLOUD_FOUNDRY_FOUNDATION")
    return {"bindingId": f"{subject}-{role}", "scopeType": scope_type, "subjectId": subject,
            "subjectType": subject_type, "role": {"name": role},
            "user": user or None, "group": None, "scopingEntity": scope}


BINDINGS = [
    binding("alice@corp", SPACE, "space_developer", username="alice", email="alice@corp"),
    binding("alice@corp", ORG, "organization_manager", username="alice", email="alice@corp"),
    binding("bob@corp", OTHER_SPACE, "space_developer", username="bob"),
    binding("ci-client", SPACE, "space_developer", subject_type="OAUTH_APP"),
    binding("ops", FOUNDATION, "organization_user", subject_type="GROUP"),
]


async def test_list_users_groups_cf_roles_by_subject():
    svc = service(BINDINGS)
    result = await svc.list_users()
    assert "queryAllRoleBindings" in svc.hub.documents[-1]
    assert [(u["type"], u["name"]) for u in result["items"]] == [
        ("client", "ci-client"), ("group", "ops"), ("user", "alice"), ("user", "bob")]
    alice = result["items"][2]
    assert alice["roles"] == [
        {"role": "organization_manager", "scope": "organization", "foundation": "tas-east",
         "org": "payments", "space": None},
        {"role": "space_developer", "scope": "space", "foundation": "tas-east",
         "org": "payments", "space": "prod"},
    ]
    assert result["role_bindings_scanned"] == 5


async def test_list_users_filters_by_org_and_space():
    by_org = await service(BINDINGS).list_users(org="payments")
    assert [u["name"] for u in by_org["items"]] == ["ci-client", "alice"]
    by_space = await service(BINDINGS).list_users(org="retail", space="prod")
    assert [u["name"] for u in by_space["items"]] == ["bob"]
    assert by_space["filtered_client_side"] == ["org", "space"]
