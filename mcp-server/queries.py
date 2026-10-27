"""GraphQL documents for the Tanzu Hub inventory graph (`entityQuery.queryEntities`).

Shapes follow the documented examples in the Hub GraphQL reference: `properties { name value }`,
`entitiesIn` / `entitiesOut(entityType:, first:)`, `entitiesTraversalFilter`, and cursor
pagination through `pageInfo { hasNextPage endCursor }`. Child-to-parent edges are OUT
(Application -> Foundation, ServicePlanGroup -> ServiceOfferingGroup in the docs), so the
assumed chain is ServiceInstance -> Space -> Organization -> Foundation.

Filter values are rendered as GraphQL string literals rather than typed variables, so the
documents do not depend on the names of Hub's filter input types. Only `first` / `after`
are variables. Run `uv run python schema_check.py` to validate every document against the
live schema.
"""

from __future__ import annotations

import json

from config import Settings

ORGANIZATION = "Tanzu.TAS.Organization"
SPACE = "Tanzu.TAS.Space"
FOUNDATION = "Tanzu.TAS.Foundation"
SERVICE_INSTANCE = "Tanzu.TAS.ServiceInstance"
SERVICE_OFFERING = "Tanzu.TAS.ServiceOffering"
SERVICE_PLAN = "Tanzu.TAS.ServicePlan"
APPLICATION = "Tanzu.TAS.Application"
DEPLOYMENT = "Tanzu.TAS.Deployment"
BOSH_VM = "Tanzu.TAS.BoshVM"

BASE_FIELDS = "entityId entityName entityType lastUpdateTime properties { name value }"
REF_FIELDS = "entityId entityName"
PAGE_FIELDS = "pageInfo { hasNextPage endCursor } totalCount"


def literal(value: str) -> str:
    """Render a GraphQL string literal; JSON string escapes are valid GraphQL escapes."""
    if not isinstance(value, str):
        raise TypeError("GraphQL literals must be strings")
    return json.dumps(value)


def eq_filter(settings: Settings, field: str, value: str) -> str:
    return settings.filter_template % {"field": literal(field), "value": literal(value)}


def traversal_filter(settings: Settings, entity_type: str, name: str) -> str:
    target = literal(entity_type)
    condition = eq_filter(settings, "entityName", name)
    return (
        f"{{ entitiesTraversal: [{{ direction: OUT, entityType: [{target}], "
        f"filter: {condition} }}] }}"
    )


def _out(alias: str, entity_type: str, fields: str = REF_FIELDS, first: int = 1) -> str:
    # Aliases are required: sibling entitiesOut fields with different arguments cannot merge.
    return (
        f"{alias}: entitiesOut(entityType: {literal(entity_type)}, first: {first}) "
        f"{{ entities {{ {fields} }} }}"
    )


def _query_entities(
    name: str,
    entity_type: str,
    selection: str,
    *,
    filter_literal: str | None = None,
    traversal_literal: str | None = None,
    entity_ids: list[str] | None = None,
    paginated: bool = True,
) -> str:
    args = [f"entityType: [{literal(entity_type)}]"]
    if entity_ids:
        args.append("entityId: [" + ", ".join(literal(i) for i in entity_ids) + "]")
    if filter_literal:
        args.append(f"filter: {filter_literal}")
    if traversal_literal:
        args.append(f"entitiesTraversalFilter: {traversal_literal}")
    args.append('sort: [{ field: "entityName", order: ASC }]')
    var_defs = ""
    if paginated:
        args[0:0] = ["first: $first", "after: $after"]
        var_defs = "($first: Int, $after: String)"
    return (
        f"query {name}{var_defs} {{\n"
        f"  entityQuery {{\n"
        f"    queryEntities({', '.join(args)}) {{\n"
        f"      entities {{ {selection} }}\n"
        f"      {PAGE_FIELDS}\n"
        f"    }}\n"
        f"  }}\n"
        f"}}\n"
    )


FOUNDATION_REF = _out("foundation", FOUNDATION)
ORG_CHAIN = _out("organization", ORGANIZATION, f"{REF_FIELDS} {FOUNDATION_REF}")
SPACE_CHAIN = _out("space", SPACE, f"{REF_FIELDS} {ORG_CHAIN}")
NEIGHBOUR_FIELDS = "entityId entityName entityType properties { name value }"


def list_organizations(settings: Settings, foundation: str | None = None) -> str:
    traversal = traversal_filter(settings, FOUNDATION, foundation) if foundation else None
    return _query_entities(
        "ListOrganizations", ORGANIZATION, f"{BASE_FIELDS} {FOUNDATION_REF}",
        traversal_literal=traversal,
    )


def list_spaces(settings: Settings, org: str | None = None) -> str:
    traversal = traversal_filter(settings, ORGANIZATION, org) if org else None
    return _query_entities("ListSpaces", SPACE, f"{BASE_FIELDS} {ORG_CHAIN}",
                           traversal_literal=traversal)


def list_cf_role_bindings() -> str:
    """Cloud Foundry roles as Hub knows them (provider TP4CF), with each scope's org/foundation.

    A space scope carries `organization { foundation }`; an org scope carries `foundation`.
    """
    scope = f"entityId entityName entityType {ORG_CHAIN} {FOUNDATION_REF}"
    return (
        "query ListCfRoleBindings($first: Int, $after: String) {\n"
        "  authQuery {\n"
        "    queryAllRoleBindings(first: $first, after: $after, provider: [TP4CF], scopeType: "
        "[CLOUD_FOUNDRY_FOUNDATION, CLOUD_FOUNDRY_ORGANIZATION, CLOUD_FOUNDRY_SPACE]) {\n"
        "      roleBindings { bindingId scopeType subjectId subjectType role { name } "
        f"user {{ username email origin }} group {{ groupName }} scopingEntity {{ {scope} }} }}\n"
        "      pageInfo { hasNextPage endCursor } totalCount\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def _service_instance_selection(settings: Settings, with_insights: bool) -> str:
    parts = [
        BASE_FIELDS,
        SPACE_CHAIN,
        _out("plan", SERVICE_PLAN, f"{REF_FIELDS} properties {{ name value }}"),
        _out("offering", SERVICE_OFFERING, f"{REF_FIELDS} properties {{ name value }}"),
    ]
    if with_insights and settings.insights_selection:
        parts.append(settings.insights_selection)
    return " ".join(parts)


def list_service_instances(
    settings: Settings, space: str | None = None, with_insights: bool = False
) -> str:
    traversal = traversal_filter(settings, SPACE, space) if space else None
    return _query_entities(
        "ListServiceInstances", SERVICE_INSTANCE,
        _service_instance_selection(settings, with_insights),
        traversal_literal=traversal,
    )


BOSH_NODES = (
    f'deployment: entitiesOut(entityType: {literal(DEPLOYMENT)}, first: 5) {{ entities {{ '
    f'{BASE_FIELDS} vms: entitiesIn(entityType: {literal(BOSH_VM)}, first: 50) '
    f'{{ entities {{ {BASE_FIELDS} }} }} }} }}'
)


def service_instance_details(
    settings: Settings,
    *,
    entity_id: str | None = None,
    guid: str | None = None,
    name: str | None = None,
    with_nodes: bool = False,
) -> str:
    """Single-instance lookup with direction-agnostic neighbours for classification.

    `with_nodes` adds the instance's BOSH deployment and its VMs (on-demand services only).
    """
    parts = [
        _service_instance_selection(settings, with_insights=True),
        f"relatedIn: entitiesIn(first: 100) {{ entities {{ {NEIGHBOUR_FIELDS} }} }}",
        f"relatedOut: entitiesOut(first: 100) {{ entities {{ {NEIGHBOUR_FIELDS} }} }}",
    ]
    if with_nodes:
        parts.append(BOSH_NODES)
    selection = " ".join(parts)
    filter_literal = None
    if guid:
        filter_literal = eq_filter(settings, settings.guid_field, guid)
    elif name:
        filter_literal = eq_filter(settings, "entityName", name)
    return _query_entities(
        "ServiceInstanceDetails", SERVICE_INSTANCE, selection,
        filter_literal=filter_literal,
        entity_ids=[entity_id] if entity_id else None,
    )


def connectivity_check() -> str:
    return _query_entities("ConnectivityCheck", FOUNDATION, REF_FIELDS)


def sample_documents(settings: Settings) -> dict[str, str]:
    """Every document shape the server can send, for schema validation."""
    return {
        "list_organizations": list_organizations(settings),
        "list_organizations_by_foundation": list_organizations(settings, "f"),
        "list_spaces": list_spaces(settings),
        "list_spaces_by_org": list_spaces(settings, "o"),
        "list_users": list_cf_role_bindings(),
        "list_service_instances": list_service_instances(settings),
        "list_service_instances_by_space": list_service_instances(settings, "s"),
        "list_services_status": list_service_instances(settings, with_insights=True),
        "get_service_instance_by_id": service_instance_details(settings, entity_id="vrn/x"),
        "get_service_instance_by_guid": service_instance_details(settings, guid="g"),
        "get_service_instance_by_name": service_instance_details(settings, name="n"),
        "get_mysql_status": service_instance_details(settings, guid="g", with_nodes=True),
        "connectivity_check": connectivity_check(),
    }
