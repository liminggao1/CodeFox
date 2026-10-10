"""Skill 加载测试：清单解析与目录行为。"""

from __future__ import annotations

from agent_harness import core
from agent_harness.context import skills
import pytest

def test_catalog_lists_every_shipped_skill(host_factory) -> None:
    host = host_factory()
    catalog = skills.list_skills()

    for name in ("agent-builder", "code-review", "mcp-builder", "pdf"):
        assert f"- {name}:" in catalog


def test_load_skill_returns_the_full_manifest(host_factory) -> None:
    host = host_factory()

    content = skills.load_skill("pdf")

    assert content.startswith("---")
    assert len(content) > len(skills.SKILL_REGISTRY["pdf"]["description"])


def test_unknown_skill_lists_available_names(host_factory) -> None:
    host = host_factory()

    with pytest.raises(ValueError) as error:
        skills.load_skill("nope")
    result = str(error.value)

    assert result.startswith("Skill not found: nope")
    assert "agent-builder" in result


def test_frontmatter_requires_standalone_delimiters(host_factory) -> None:
    host = host_factory()

    meta, body = skills._parse_frontmatter(
        "---\nname: demo\ndescription: d\n---\nBody text"
    )
    assert meta == {"name": "demo", "description": "d"}
    assert body == "Body text"

    meta, body = skills._parse_frontmatter("---\nname: demo\nBody text")
    assert meta == {}
    assert body == "---\nname: demo\nBody text"


def test_frontmatter_falls_back_on_invalid_yaml(host_factory) -> None:
    host = host_factory()

    meta, body = skills._parse_frontmatter(
        "---\n: : :\n---\nBody text"
    )

    assert meta == {}
    assert body == "Body text"


def test_skill_scan_uses_the_directory_name_when_metadata_is_missing(
    host_factory, tmp_path, monkeypatch
) -> None:
    host = host_factory()
    skills_dir = tmp_path / "skills"
    manifest_dir = skills_dir / "my-skill"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "SKILL.md").write_text(
        "# Heading line\n\nDetails", encoding="utf-8"
    )
    monkeypatch.setattr(core, "SKILLS_DIR", skills_dir)

    skills.scan_skills()

    entry = skills.SKILL_REGISTRY["my-skill"]
    assert entry["name"] == "my-skill"
    assert entry["description"] == "Heading line"
import agent_harness.context.skills as skills
