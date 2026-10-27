---
name: tanzu-hub-platform
description: Workflows for answering Cloud Foundry questions with the read-only Tanzu Hub MCP tools - what is unhealthy, the status of a database or MySQL cluster, who has access to an org or space, and where a service instance lives - across all foundations. Use for any question about orgs, spaces, user roles, service instances or service health.
---

# Tanzu Hub platform questions

The tools are read-only and cover every foundation attached to Tanzu Hub. Discover exact names
with the `list_*` tools before filtering; names are case-sensitive in Hub.

## Workflows

### "What is broken?" (health triage)
1. `list_services_status(only_unhealthy=true)`, optionally with `space`, `org` or `offering`.
2. Report `summary` counts first, then each item worst first: name, foundation/org/space,
   `status` and `reasons`.
3. For each `failed` or `degraded` item that matters, drill down:
   - MySQL (`offering` is `p.mysql`): `get_mysql_service_instance_status(<guid>)`.
   - Anything else: `get_service_instance(<guid>)`.
4. If nothing is unhealthy, say so with the total checked (`total_in_hub`).

### "Is database X OK?" (MySQL deep dive)
1. `get_mysql_service_instance_status(<name, GUID or entity_id>)`. If it returns `candidates`,
   the name is ambiguous: show them and ask which one, or retry with the GUID.
2. Read, in order: `status` (overall), `hub_health` (last operation and alerts),
   `bosh_nodes` (per VM), `topology` (replication and proxy, often unavailable).
3. Answer: verdict, then nodes as `mysql/0 healthy (az1, 10.0.0.1)`, then what is unknown.

### "Who has access to X?" (access audit)
1. `list_users(org=<org>)` or `list_users(org=<org>, space=<space>)`.
2. Group the answer by person: name (email), then roles as `space_developer in org/space`.
   Show groups and clients separately; clients appear under their OAuth client ID.
3. Always add: "roles granted directly with the cf CLI may not be listed; `cf org-users <org>`
   or `cf space-users <org> <space>` shows the full list."

### "Where is X / what runs in Y?" (inventory)
- Orgs: `list_organizations`. Spaces of an org: `list_spaces(org=...)`.
- Instances in a space: `list_service_instances(space=...)` (space filtering runs in Hub, so
  prefer it over `org` alone on large estates).
- One instance by name, GUID or `vrn/...` entity_id: `get_service_instance`. Its `bound_apps`
  lists the apps using it.

## Reading results

- `status` is one of `healthy`, `in_progress`, `degraded`, `failed`, `unknown`; `reasons`
  says why. `in_progress` means a create/update/delete is running, not a fault.
- Service health comes from the last operation. Alerts only count when
  `alerts_source` is `hub-insights`; with `not-configured`, a `healthy` instance means "last
  operation succeeded", not "no incidents". Say so when it matters.
- MySQL `bosh_nodes`: a node is healthy when its VM is `started` and its agent responds. Some
  nodes down means `degraded` (an HA cluster still serves); all down means `failed`.
  `node_count: 0` means Hub knows no dedicated VMs for this instance.
- `topology.available: false` means replication and proxy state are unknown. Never infer
  primary/standby or replication lag from `bosh_nodes`.
- `truncated: true`: the list is partial; narrow by space or org. `filtered_client_side`
  lists filters applied after fetching, which is why `count` can be lower than `total_in_hub`.
- An `error` field (not found, ambiguous) is an answer, not a failure: relay it. A tool error
  means Hub was unreachable or refused the request; say so and do not guess.

## Out of scope

- Changing anything (restart, scale, bind, rotate credentials): explain the steps or the
  `cf` command for a human; do not claim to act.
- Application instances, logs and metrics: not available from these tools.
