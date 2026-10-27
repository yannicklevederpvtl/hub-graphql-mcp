from pathlib import Path

import pytest
from conftest import make_settings
from graphql.language import Visitor

import queries
from graphql import StringValueNode, build_schema, parse, validate, visit
from hub_client import assert_query_only

SCHEMA = build_schema((Path(__file__).parent / "hub_schema.graphql").read_text())


@pytest.mark.parametrize("name,document", queries.sample_documents(make_settings()).items())
def test_documents_validate_against_documented_shape(name, document):
    errors = validate(SCHEMA, parse(document))
    assert errors == [], f"{name}: {[e.message for e in errors]}"
    assert_query_only(document)


def test_alternative_filter_template_validates():
    settings = make_settings(
        filter_template="{ field: %(field)s, operator: EQUALS, value: %(value)s }"
    )
    for document in queries.sample_documents(settings).values():
        assert validate(SCHEMA, parse(document)) == []


def string_values(document: str) -> list[str]:
    found: list[str] = []

    class Collect(Visitor):
        def enter_string_value(self, node: StringValueNode, *_):
            found.append(node.value)

    visit(parse(document), Collect())
    return found


@pytest.mark.parametrize(
    "hostile",
    [
        'x" }) { entityId } } } mutation { delete(id: "1") { ok',
        'line\nbreak\t"quoted" \\ back',
        "unicode ünïcødé 🚀",
    ],
)
def test_user_values_stay_inside_string_literals(hostile):
    document = queries.list_spaces(make_settings(), org=hostile)
    assert hostile in string_values(document)
    assert_query_only(document)
    assert validate(SCHEMA, parse(document)) == []


def test_literal_rejects_non_strings():
    with pytest.raises(TypeError):
        queries.literal(42)  # type: ignore[arg-type]


def test_insights_selection_is_only_added_when_configured():
    plain = queries.list_service_instances(make_settings(), with_insights=True)
    assert "insights" not in plain
    configured = queries.list_service_instances(
        make_settings(insights_selection="insights { alerts { name severity state } }"),
        with_insights=True,
    )
    assert "insights { alerts" in configured
