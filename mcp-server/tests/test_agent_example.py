"""Keep the example agent in ../agent consistent with the tools this server exposes."""

import re
from pathlib import Path

import pytest
from conftest import FakeHub, make_settings
from fastmcp import Client

from mcp_tools import build_mcp
from services import HubService

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"
SKILLS = sorted((AGENT / ".agents" / "skills").glob("*/SKILL.md"))
TOOL_CALL = re.compile(r"\b((?:list|get)_[a-z_]+)(?:\(([^)]*)\))?")

pytestmark = pytest.mark.skipif(not AGENT.is_dir(), reason="agent example not present")


async def tool_parameters() -> dict[str, set[str]]:
    service = HubService(FakeHub(), make_settings())
    async with Client(build_mcp(service)) as client:
        tools = await client.list_tools()
    return {t.name: set(t.input_schema.get("properties", {})) for t in tools}


def frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md needs YAML frontmatter"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines() if ": " in line)


def test_skills_exist_with_name_and_description():
    assert SKILLS
    for path in SKILLS:
        meta = frontmatter(path.read_text())
        assert meta.get("name") == path.parent.name
        assert len(meta.get("description", "")) > 40


async def test_skill_and_agents_md_only_use_real_tools_and_arguments():
    parameters = await tool_parameters()
    for path in [*SKILLS, AGENT / "AGENTS.md"]:
        for tool, args in TOOL_CALL.findall(path.read_text()):
            assert tool in parameters, f"{path.name} mentions unknown tool {tool}"
            for arg in re.findall(r"(\w+)=", args or ""):
                assert arg in parameters[tool], f"{path.name}: {tool} has no argument {arg}"


async def test_every_tool_is_covered_by_a_skill():
    mentioned = {tool for path in SKILLS for tool, _ in TOOL_CALL.findall(path.read_text())}
    assert set(await tool_parameters()) <= mentioned


def test_agents_md_points_to_the_skill():
    agents_md = (AGENT / "AGENTS.md").read_text()
    for path in SKILLS:
        assert f"`{path.parent.name}`" in agents_md


def test_deploy_script_matches_manifests():
    deploy = (REPO / "scripts" / "deploy.sh").read_text()
    agent_manifest = (AGENT / "manifest.yml").read_text()
    server_manifest = (REPO / "mcp-server" / "manifest.yml").read_text()
    for service in ("hub-platform-model", "hub-graphql-mcp-tools"):
        assert service in agent_manifest and service in deploy
    assert "- hub-graphql-mcp-creds" in server_manifest and "hub-graphql-mcp-creds" in deploy
    assert "route: hub-graphql-mcp.apps.internal" in server_manifest
    assert "agent_buildpack" in agent_manifest
