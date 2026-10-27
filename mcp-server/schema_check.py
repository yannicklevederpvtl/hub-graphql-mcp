"""Validate this server's GraphQL documents against the live Tanzu Hub schema.

    uv run python schema_check.py            # human-readable report
    uv run python schema_check.py --json     # machine-readable report

Uses the same environment as the server. To bootstrap before graphql/auth.graphql is filled
in, copy a bearer token from Altair and run with HUB_AUTH_MODE=static HUB_STATIC_TOKEN=...
Exits non-zero when any document fails validation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any

import queries
from config import load_settings
from graphql import (
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLObjectType,
    GraphQLSchema,
    build_client_schema,
    get_introspection_query,
    get_named_type,
    parse,
    validate,
    validate_schema,
)
from hub_client import HubClient, HubError

INTERESTING_TYPES = re.compile(r"user|rolebinding|insight|alert|metric|stat", re.I)
AUTH_FIELDS = re.compile(r"token|auth|login|serviceaccount", re.I)
ENTITY_TYPES = (
    queries.FOUNDATION, queries.ORGANIZATION, queries.SPACE,
    queries.SERVICE_INSTANCE, queries.SERVICE_OFFERING, queries.SERVICE_PLAN,
    queries.APPLICATION, queries.DEPLOYMENT, queries.BOSH_VM,
)


def tolerate_schema_defects(schema: GraphQLSchema) -> list[str]:
    """Hub's schema has spec nits (e.g. deprecated interface implementations) that graphql-core
    refuses to validate against; record them and validate the documents anyway."""
    defects = [error.message for error in validate_schema(schema)]
    schema._validation_errors = []  # noqa: SLF001
    return defects


def validate_documents(schema: GraphQLSchema, documents: dict[str, str]) -> dict[str, list[str]]:
    return {
        name: [error.message for error in validate(schema, parse(document))]
        for name, document in documents.items()
    }


def describe_schema(schema: GraphQLSchema) -> dict[str, Any]:
    report: dict[str, Any] = {}
    query_type = schema.query_type
    entity_query = query_type.fields.get("entityQuery") if query_type else None
    if entity_query:
        entity_query_type = get_named_type(entity_query.type)
        query_entities = (
            entity_query_type.fields.get("queryEntities")
            if isinstance(entity_query_type, GraphQLObjectType) else None
        )
        if query_entities:
            report["queryEntities_args"] = {
                name: str(arg.type) for name, arg in query_entities.args.items()
            }
            filter_arg = query_entities.args.get("filter")
            if filter_arg:
                filter_type = get_named_type(filter_arg.type)
                if isinstance(filter_type, GraphQLInputObjectType):
                    report["filter_input_fields"] = {
                        n: str(f.type) for n, f in filter_type.fields.items()
                    }
                    for field in filter_type.fields.values():
                        named = get_named_type(field.type)
                        if isinstance(named, GraphQLEnumType):
                            report.setdefault("filter_enums", {})[named.name] = list(named.values)
            connection = get_named_type(query_entities.type)
            if isinstance(connection, GraphQLObjectType) and "entities" in connection.fields:
                entity = get_named_type(connection.fields["entities"].type)
                if isinstance(entity, GraphQLObjectType):
                    report["entity_fields"] = sorted(entity.fields)
    else:
        report["warning"] = "Query.entityQuery not found"
    mutation_type = schema.mutation_type
    if mutation_type:
        report["auth_like_mutations"] = sorted(
            f"{name}: {get_named_type(field.type).name}"
            for name, field in mutation_type.fields.items()
            if AUTH_FIELDS.search(name)
        )
    report["interesting_types"] = sorted(
        name for name in schema.type_map
        if not name.startswith("__") and INTERESTING_TYPES.search(name)
    )[:200]
    return report


async def entity_counts(hub: HubClient, entity_types: list[str]) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    for entity_type in entity_types:
        document = (
            f"query Count {{ entityQuery {{ queryEntities(entityType: "
            f"[{queries.literal(entity_type)}], first: 1) "
            "{ totalCount entities { entityId } } } }"
        )
        try:
            data = await hub.query(document)
            counts[entity_type] = data["entityQuery"]["queryEntities"].get("totalCount")
        except HubError as exc:
            counts[entity_type] = f"error: {exc}"
    return counts


async def run() -> dict[str, Any]:
    # The CLI serves nothing, so the MCP endpoint API key is irrelevant here.
    os.environ.setdefault("ALLOW_UNAUTHENTICATED_MCP", "true")
    settings = load_settings()
    hub = HubClient(settings)
    try:
        data = await hub.query(get_introspection_query(descriptions=False))
        schema = build_client_schema(data)
        return {
            "hub_url": settings.hub_url,
            "schema_defects": tolerate_schema_defects(schema),
            "validation": validate_documents(schema, queries.sample_documents(settings)),
            "schema": describe_schema(schema),
            "entity_counts": await entity_counts(hub, list(ENTITY_TYPES)),
        }
    finally:
        await hub.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()
    try:
        report = asyncio.run(run())
    except HubError as exc:
        print(f"Hub error: {exc}", file=sys.stderr)
        return 2
    failures = {name: errors for name, errors in report["validation"].items() if errors}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Hub: {report['hub_url']}\n")
        if report["schema_defects"]:
            print(f"Hub schema spec defects (ignored): {len(report['schema_defects'])}\n")
        print("Document validation:")
        for name, errors in report["validation"].items():
            print(f"  {'FAIL' if errors else 'ok  '} {name}")
            for error in errors:
                print(f"         - {error}")
        print("\nEntity counts:")
        for entity_type, count in report["entity_counts"].items():
            print(f"  {entity_type}: {count}")
        print("\nSchema details:")
        print(json.dumps(report["schema"], indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
