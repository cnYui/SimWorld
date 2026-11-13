"""The SimWorld skill: the page rules an AI client follows when it designs through the MCP tools."""
from __future__ import annotations

from pathlib import Path

SKILL_DIR = Path(__file__).parent / 'skills' / 'simworld'


def skill_text():
    """The SimWorld skill (page rules) without its front matter."""
    text = (SKILL_DIR / 'SKILL.md').read_text(encoding='utf8')
    return text.split('---', 2)[2].lstrip() if text.startswith('---') else text


def install_skill(target):
    """Copy the skill where an AI client loads skills: 'claude' (~/.claude/skills), 'codex'
    (~/.codex/skills) or a directory. Returns the written SKILL.md path."""
    base = {'claude': Path.home() / '.claude' / 'skills', 'codex': Path.home() / '.codex' / 'skills'}.get(target)
    dest = (base if base is not None else Path(target)) / 'simworld'
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / 'SKILL.md'
    out.write_text((SKILL_DIR / 'SKILL.md').read_text(encoding='utf8'), encoding='utf8')
    return out
