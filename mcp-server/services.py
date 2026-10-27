from __future__ import annotations

import logging
from typing import Any

import queries
from config import Settings
from entities import (
    UUID,
    first_related,
    guid_of,
    name_of,
    properties,
    ref,
    related,
    same_name,
)
from health import bosh_vm_status, combine_statuses, extract_alerts, normalize, summarize_vms
from hub_client import HubClient, dig
from mysql_deep import MySQLDeepStatus

logger = logging.getLogger("hub_graphql_mcp.services")

KNOWN_ENTITY_KEYS = {
    "entityId", "entityName", "entityType", "lastUpdateTime", "properties",
    "space", "plan", "offering", "organization", "foundation", "relatedIn", "relatedOut",
    "deployment",
}
DETAIL_LIMIT = 5


def envelope(
    items: list[dict], truncated: bool, total: int | None, **extra: Any
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": "tanzu-hub",
        "count": len(items),
        "total_in_hub": total,
        "truncated": truncated,
        "items": items,
    }
    result.update({k: v for k, v in extra.items() if v is not None})
    return result


class HubService:
    def __init__(
        self, hub: HubClient, settings: Settings, deep: MySQLDeepStatus | None = None
    ) -> None:
        self.hub = hub
        self.settings = settings
        self.deep = deep

    async def check_connectivity(self) -> dict[str, Any]:
        entities, _, total = await self.hub.paginate(queries.connectivity_check(), limit=1)
        return {"hub": "reachable", "foundations": total, "sample": [ref(e) for e in entities]}

    async def list_organizations(self, foundation: str | None = None) -> dict[str, Any]:
        entities, truncated, total = await self.hub.paginate(
            queries.list_organizations(self.settings, foundation)
        )
        items = [
            {
                "name": e.get("entityName"),
                "entity_id": e.get("entityId"),
                "guid": guid_of(e),
                "foundation": name_of(first_related(e, "foundation")),
            }
            for e in entities
        ]
        return envelope(items, truncated, total)

    async def list_spaces(
        self, org: str | None = None, foundation: str | None = None
    ) -> dict[str, Any]:
        entities, truncated, total = await self.hub.paginate(
            queries.list_spaces(self.settings, org)
        )
        items = []
        for e in entities:
            organization = first_related(e, "organization")
            item = {
                "name": e.get("entityName"),
                "entity_id": e.get("entityId"),
                "guid": guid_of(e),
                "org": name_of(organization),
                "foundation": name_of(first_related(organization, "foundation")),
            }
            if foundation and not same_name(item["foundation"], foundation):
                continue
            items.append(item)
        return envelope(
            items, truncated, total,
            filtered_client_side=["foundation"] if foundation else None,
        )

    async def list_users(self, org: str | None = None, space: str | None = None) -> dict[str, Any]:
        """Users, groups and clients holding Cloud Foundry roles, from Hub role bindings."""
        bindings, truncated, total = await self.hub.paginate(
            queries.list_cf_role_bindings(),
            connection_path=("authQuery", "queryAllRoleBindings"),
            items_key="roleBindings",
        )
        subjects: dict[tuple[str, str], dict[str, Any]] = {}
        for binding in bindings:
            role = _cf_role(binding)
            if org and not same_name(role["org"], org):
                continue
            if space and not same_name(role["space"], space):
                continue
            user = binding.get("user") or {}
            group = binding.get("group") or {}
            subject_type = (binding.get("subjectType") or "").lower()
            key = (subject_type, binding.get("subjectId") or "")
            subject = subjects.setdefault(key, {
                "name": user.get("username") or group.get("groupName") or key[1],
                "type": {"oauth_app": "client"}.get(subject_type, subject_type),
                "subject_id": key[1],
                "email": user.get("email"),
                "origin": user.get("origin"),
                "roles": [],
            })
            subject["roles"].append(role)
        items = sorted(subjects.values(), key=lambda s: (s["type"], (s["name"] or "").lower()))
        for item in items:
            item["roles"].sort(key=lambda r: (r["foundation"] or "", r["org"] or "",
                                              r["space"] or "", r["role"] or ""))
        return envelope(
            items, truncated, total,
            role_bindings_scanned=len(bindings),
            note="Cloud Foundry roles as recorded in Hub (provider TP4CF)",
            filtered_client_side=[f for f, v in (("org", org), ("space", space)) if v] or None,
        )

    async def list_service_instances(
        self,
        org: str | None = None,
        space: str | None = None,
        offering: str | None = None,
        foundation: str | None = None,
    ) -> dict[str, Any]:
        entities, truncated, total = await self.hub.paginate(
            queries.list_service_instances(self.settings, space)
        )
        items = [
            self._service_summary(e) for e in entities
            if self._matches(e, org=org, offering=offering, foundation=foundation)
        ]
        client_side = [f for f, v in (("org", org), ("offering", offering),
                                      ("foundation", foundation)) if v]
        return envelope(items, truncated, total, filtered_client_side=client_side or None)

    async def get_service_instance(self, identifier: str) -> dict[str, Any]:
        found = await self._lookup(identifier)
        if "entity" not in found:
            return found
        return self._service_detail(found["entity"])

    async def list_services_status(
        self,
        org: str | None = None,
        space: str | None = None,
        offering: str | None = None,
        only_unhealthy: bool = False,
    ) -> dict[str, Any]:
        entities, truncated, total = await self.hub.paginate(
            queries.list_service_instances(self.settings, space, with_insights=True)
        )
        alerts_enabled = bool(self.settings.insights_selection)
        items = []
        for e in entities:
            if not self._matches(e, org=org, offering=offering):
                continue
            health = normalize(properties(e), self._alerts(e) if alerts_enabled else None)
            if only_unhealthy and health["status"] == "healthy":
                continue
            items.append({**self._service_summary(e), **health})
        items.sort(key=lambda i: (-_severity(i["status"]), i.get("name") or ""))
        summary: dict[str, int] = {}
        for item in items:
            summary[item["status"]] = summary.get(item["status"], 0) + 1
        client_side = [f for f, v in (("org", org), ("offering", offering)) if v]
        return envelope(
            items, truncated, total,
            summary=summary,
            alerts_source="hub-insights" if alerts_enabled else "not-configured",
            filtered_client_side=client_side or None,
        )

    async def get_mysql_service_instance_status(self, identifier: str) -> dict[str, Any]:
        found = await self._lookup(identifier, with_nodes=True)
        if "entity" not in found:
            return found
        entity = found["entity"]
        detail = self._service_detail(entity)
        offering_name = (detail.get("offering") or {}).get("name") or ""
        deployments = related(entity, "deployment")
        bosh = summarize_vms([_bosh_vm(vm) for d in deployments for vm in related(d, "vms")])
        result: dict[str, Any] = {
            "source": "tanzu-hub",
            "service_instance": {k: detail.get(k) for k in (
                "name", "entity_id", "guid", "org", "space", "foundation", "offering", "plan")},
            "hub_health": detail["health"],
            "bosh_deployment": deployments[0].get("entityName") if deployments else None,
            "bosh_nodes": bosh,
            "topology": None,
        }
        if "mysql" not in offering_name.lower() and "mysql" not in str(
                detail["properties"].get("ServiceType", "")).lower():
            result["warning"] = f"offering '{offering_name or 'unknown'}' does not look like MySQL"
        statuses = [detail["health"]["status"], bosh["status"]]
        if self.deep and detail.get("guid"):
            deep = await self.deep.fetch(detail["guid"])
            result["topology"] = deep
            if deep.get("available"):
                statuses.append(deep.get("status", "unknown"))
        elif self.settings.mysql_deep_status:
            result["topology"] = {"available": False, "reason": "service instance GUID unknown"}
        else:
            result["topology"] = {
                "available": False,
                "reason": "live topology disabled (ENABLE_MYSQL_DEEP_STATUS=false); Hub reports "
                          "BOSH VM state but not proxy backend or replication state",
            }
        result["status"] = combine_statuses(*statuses)
        return result

    async def _lookup(self, identifier: str, with_nodes: bool = False) -> dict[str, Any]:
        identifier = identifier.strip()
        if not identifier:
            return {"source": "tanzu-hub", "error": "identifier is required"}
        if identifier.startswith("vrn/"):
            match_on, key = "entity_id", {"entity_id": identifier}
        elif UUID.match(identifier):
            match_on, key = "guid", {"guid": identifier}
        else:
            match_on, key = "name", {"name": identifier}
        document = queries.service_instance_details(self.settings, with_nodes=with_nodes, **key)
        data = await self.hub.query(document, {"first": DETAIL_LIMIT, "after": None})
        entities = dig(data, ("entityQuery", "queryEntities")).get("entities") or []
        if not entities:
            return {"source": "tanzu-hub", "error": f"no service instance found by {match_on}",
                    "identifier": identifier}
        if len(entities) > 1:
            return {
                "source": "tanzu-hub",
                "error": f"{len(entities)} service instances match this {match_on}; "
                         "retry with a GUID or Hub entity_id",
                "candidates": [self._service_summary(e) for e in entities],
            }
        return {"entity": entities[0]}

    def _alerts(self, entity: dict) -> list[dict[str, Any]]:
        extra = {k: v for k, v in entity.items() if k not in KNOWN_ENTITY_KEYS}
        return extract_alerts(extra)

    def _matches(
        self, entity: dict, *, org: str | None = None, offering: str | None = None,
        foundation: str | None = None,
    ) -> bool:
        organization = first_related(entity, "space", "organization")
        if org and not same_name(name_of(organization), org):
            return False
        if offering and not same_name(_offering_name(entity), offering):
            return False
        return not (
            foundation
            and not same_name(name_of(first_related(organization, "foundation")), foundation)
        )

    def _service_summary(self, entity: dict) -> dict[str, Any]:
        space = first_related(entity, "space")
        organization = first_related(space, "organization")
        props = properties(entity)
        return {
            "name": entity.get("entityName"),
            "entity_id": entity.get("entityId"),
            "guid": guid_of(entity),
            "org": name_of(organization),
            "space": name_of(space),
            "foundation": name_of(first_related(organization, "foundation")),
            "offering": _offering_name(entity),
            "plan": name_of(first_related(entity, "plan")),
            "last_operation": normalize(props, None)["last_operation"],
        }

    def _service_detail(self, entity: dict) -> dict[str, Any]:
        summary = self._service_summary(entity)
        neighbours = related(entity, "relatedOut") + related(entity, "relatedIn")
        by_type: dict[str, list[str]] = {}
        for n in neighbours:
            by_type.setdefault(n.get("entityType") or "unknown", []).append(n.get("entityName"))
        apps = sorted({n["entityName"] for n in neighbours
                       if n.get("entityType") == queries.APPLICATION})
        alerts = self._alerts(entity) if self.settings.insights_selection else None
        return {
            "source": "tanzu-hub",
            **summary,
            "offering": ref(first_related(entity, "offering")),
            "plan": ref(first_related(entity, "plan")),
            "last_update_time": entity.get("lastUpdateTime"),
            "properties": properties(entity),
            "bound_apps": apps,
            "related": {t: sorted(filter(None, names))[:25] for t, names in by_type.items()},
            "health": normalize(properties(entity), alerts),
        }


def _severity(status: str) -> int:
    return {"failed": 4, "degraded": 3, "in_progress": 2, "unknown": 1}.get(status, 0)


def _offering_name(entity: dict) -> str | None:
    # User-provided instances have no ServiceOffering entity, only a property.
    return name_of(first_related(entity, "offering")) or properties(entity).get(
        "ServiceOfferingName")


def _cf_role(binding: dict) -> dict[str, Any]:
    scope = binding.get("scopingEntity") or {}
    scope_type = (binding.get("scopeType") or "").removeprefix("CLOUD_FOUNDRY_").lower()
    organization = first_related(scope, "organization")
    if scope_type == "space":
        org, space = name_of(organization), scope.get("entityName")
        foundation = name_of(first_related(organization, "foundation"))
    elif scope_type == "organization":
        org, space = scope.get("entityName"), None
        foundation = name_of(first_related(scope, "foundation"))
    else:
        org = space = None
        foundation = scope.get("entityName")
    return {
        "role": (binding.get("role") or {}).get("name"),
        "scope": scope_type,
        "foundation": foundation,
        "org": org,
        "space": space,
    }


def _bosh_vm(vm: dict) -> dict[str, Any]:
    props = properties(vm)
    job, index = props.get("JobName") or vm.get("entityName"), props.get("Index")
    return {
        "name": f"{job}/{index}" if index is not None else job,
        "status": bosh_vm_status(props),
        "state": props.get("State"),
        "agent_responding": props.get("Status"),
        "az": props.get("AZ"),
        "ips": props.get("IPs"),
        "vm_type": props.get("VMType"),
        "persistent_disk_mb": props.get("PersistentDiskMb"),
        "stemcell": props.get("Stemcell"),
        "vm_created_at": props.get("VMCreatedAt"),
    }
