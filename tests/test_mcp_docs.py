"""The MCP 使用说明 page must describe every tool the MCP server offers."""
import asyncio

from emcosim.app.link import AppLink
from emcosim.mcp_docs import GROUPS, described, guide_markdown, mcp_guide
from emcosim.mcp_server import build_server


def _tools():
    return [(t.name, t.description) for t in asyncio.run(build_server(None, link=AppLink({})).list_tools())]


def test_every_tool_has_a_plain_language_entry():
    names = {n for n, _ in _tools()}
    assert not names - described(), f'add these tools to emcosim/mcp_docs.py GROUPS: {sorted(names - described())}'
    assert not described() - names, f'these tools no longer exist: {sorted(described() - names)}'
    for _, _, _, rows in GROUPS:
        for name, what, when in rows:
            assert what and when, name


def test_guide_lists_new_tools_and_renders_markdown():
    sections = mcp_guide([*_tools(), ('brand_new_tool', 'Does a new thing.')])
    assert sections[-1]['title'] == '其他' and sections[-1]['tools'][0]['name'] == 'brand_new_tool'
    md = guide_markdown(sections)
    assert '`add_shape`' in md and '什么时候用' in md and 'design_rules' in md.split('##')[0]
