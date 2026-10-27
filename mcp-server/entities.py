from __future__ import annotations

import re
from typing import Any

SECRET_KEY = re.compile(r"pass(word|wd)?|secret|token|credential|private.?key|api.?key", re.I)
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
REDACTED = "[REDACTED]"


def properties(entity: dict | None) -> dict[str, Any]:
    """Flatten `properties { name value }` into a dict with secret-looking values redacted."""
    if not entity:
        return {}
    raw = entity.get("properties") or []
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = ((p.get("name"), p.get("value")) for p in raw if isinstance(p, dict))
    return {
        str(name): (REDACTED if SECRET_KEY.search(str(name)) else value)
        for name, value in items
        if name is not None
    }


def prop(props: dict[str, Any], *names: str) -> Any:
    """Case-insensitive property lookup across candidate names."""
    lowered = {k.lower(): v for k, v in props.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def related(entity: dict | None, alias: str) -> list[dict]:
    if not entity:
        return []
    connection = entity.get(alias) or {}
    return [e for e in connection.get("entities") or [] if isinstance(e, dict)]


def first_related(entity: dict | None, *aliases: str) -> dict | None:
    """Follow a chain of single-valued relationship aliases, e.g. space -> organization."""
    current = entity
    for alias in aliases:
        items = related(current, alias)
        if not items:
            return None
        current = items[0]
    return current


def name_of(entity: dict | None) -> str | None:
    return entity.get("entityName") if entity else None


def guid_of(entity: dict) -> str | None:
    value = prop(properties(entity), "GUID", "guid", "Guid", "cfGuid")
    return str(value) if value else None


def ref(entity: dict | None) -> dict | None:
    if not entity:
        return None
    return {"name": entity.get("entityName"), "entity_id": entity.get("entityId")}


def same_name(left: str | None, right: str | None) -> bool:
    return bool(left and right and left.casefold() == right.casefold())
