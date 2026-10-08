"""加载随包分发的 SKILL.md 清单。

导入时扫描一次目录；系统提示词列出结果，模型通过 load_skill(name)
把指定 skill 的全文加载到上下文。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import core

# -- Skill 加载 --

SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, text

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1)
         if line.rstrip("\r\n") == "---"),
        None,
    )
    if closing_index is None:
        return {}, text

    frontmatter = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1:]).strip()
    try:
        meta = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def scan_skills():
    """扫描随包分发的 skill 目录并重建内存索引。

    返回：None。清空并填充 SKILL_REGISTRY；目录不存在时索引为空。
        读取直接子目录中的 SKILL.md，保存名称、描述和全文，跳过越界路径。
    """
    SKILL_REGISTRY.clear()
    if not core.SKILLS_DIR.exists():
        return
    skills_root = core.SKILLS_DIR.resolve()
    for directory in sorted(core.SKILLS_DIR.iterdir()):
        if not directory.is_dir():
            continue
        manifest = directory / "SKILL.md"
        if not manifest.exists():
            continue
        if not manifest.resolve().is_relative_to(skills_root):
            continue
        raw = manifest.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        raw_name = meta.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        name = name or directory.name
        raw_desc = meta.get("description")
        desc = raw_desc.strip() if isinstance(raw_desc, str) else ""
        desc = desc or body.split("\n", 1)[0].lstrip("#").strip()
        SKILL_REGISTRY[name] = {
            "name": name,
            "description": desc,
            "content": raw,
        }


scan_skills()


def list_skills() -> str:
    """生成可放入系统提示词的 skill 名称与描述目录。

    返回：每项一行的字符串；索引为空时返回无 skill 提示，不读取正文或请求模型。
    """
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- {skill['name']}: {skill['description']}"
        for skill in SKILL_REGISTRY.values())


def load_skill(name: str) -> str:
    """按已扫描名称加载 skill 全文，作为工具结果返回模型。

    输入：name 为 SKILL_REGISTRY 中的名称。
    返回：SKILL.md 原文；未找到时返回错误及可用名称，不重新扫描目录。
    """
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        available = ", ".join(SKILL_REGISTRY.keys()) or "(none)"
        return f"Skill not found: {name}. Available: {available}"
    return skill["content"]
