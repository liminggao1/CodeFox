"""Skill 加载测试：清单解析与目录行为。"""

from __future__ import annotations

from codefox import core
from codefox.context import skills
import pytest


def test_shipped_skill_catalog_load_and_unknown_name(host_factory) -> None:
    host_factory()
    for name in ("agent-builder", "code-review", "mcp-builder", "pdf"):
        assert f"- {name}:" in skills.list_skills()
        content = skills.load_skill(name)
        assert content.startswith("---")
        assert len(content) > len(skills.SKILL_REGISTRY[name]["description"])
    with pytest.raises(ValueError, match="Skill not found: nope.*agent-builder"):
        skills.load_skill("nope")


def test_skill_scan_uses_the_directory_name_when_metadata_is_missing(
    host_factory, tmp_path, monkeypatch
) -> None:
    host = host_factory()
    skills_dir = tmp_path / "skills"
    manifest_dir = skills_dir / "my-skill"
    manifest_dir.mkdir(parents=True)
    manifest = manifest_dir / "SKILL.md"
    try:
        with monkeypatch.context() as patch:
            patch.setattr(core, "SKILLS_DIR", skills_dir)
            # 清单解析通过扫描和加载接口验证，包括缺少分隔符与坏 YAML 的回退。
            for raw, name, description in [
                ("---\nname: demo\ndescription: d\n---\nBody text", "demo", "d"),
                ("# Heading line\n\nDetails", "my-skill", "Heading line"),
                ("---\nname: demo\nBody text", "my-skill", "---"),
                ("---\n: : :\n---\nBody text", "my-skill", "Body text"),
            ]:
                manifest.write_text(raw, encoding="utf-8")
                skills.scan_skills()
                assert skills.SKILL_REGISTRY[name]["description"] == description, raw
                assert skills.load_skill(name) == raw
    finally:
        skills.scan_skills()
