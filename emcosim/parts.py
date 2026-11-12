"""JLCPCB / LCSC parts lookup, built into emcosim's MCP server.

The parts data comes from PCB Parts MCP (github.com/Averyy/jlcpcb-mcp, MIT License,
Copyright (c) 2025 Avery), which its author runs as a public service at
https://pcbparts.dev/mcp: parametric search over JLCPCB's in-stock catalog, live
stock and price per LCSC number, alternatives, pinouts. emcosim connects to it as an
MCP client and re-exposes a few of its tools next to its own, so an agent connected
to emcosim gets parts search without a second server. The upstream project's live
lookups go through an anti-bot HTTP client against JLCPCB's website; emcosim does not
run that part itself. EMCOSIM_PARTS_MCP points at another instance (for example one
you host from that repository).

Results are cached in memory for 10 minutes.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

ENDPOINT = os.environ.get('EMCOSIM_PARTS_MCP', 'https://pcbparts.dev/mcp')
CREDIT = 'parts data: PCB Parts MCP (github.com/Averyy/jlcpcb-mcp, MIT) via ' + ENDPOINT
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()
TTL_S = 600


class PartsUnavailable(RuntimeError):
    pass


def _client():
    try:                                            # mcp >= 2
        from mcp.client.streamable_http import streamable_http_client as c
    except ImportError:                             # mcp 1.x
        from mcp.client.streamable_http import streamablehttp_client as c
    return c


async def _call_async(tool, args, timeout):
    from mcp import ClientSession
    async with _client()(ENDPOINT) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as s:
            await asyncio.wait_for(s.initialize(), timeout)
            res = await asyncio.wait_for(s.call_tool(tool, args), timeout)
    if getattr(res, 'structuredContent', None):
        out = res.structuredContent
        return out.get('result', out) if isinstance(out, dict) and set(out) == {'result'} else out
    text = ''.join(getattr(c, 'text', '') for c in res.content or [])
    try:
        return json.loads(text)
    except ValueError:
        return {'text': text}


def call(tool, args=None, timeout=30.0):
    """Call one tool of the parts server; returns its JSON result."""
    args = {k: v for k, v in (args or {}).items() if v is not None}
    key = tool + json.dumps(args, sort_keys=True)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < TTL_S:
            return hit[1]
    try:
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if running:                                  # called from inside an event loop (MCP server)
            box = {}
            t = threading.Thread(target=lambda: box.update(r=asyncio.run(_call_async(tool, args, timeout))))
            t.start()
            t.join(timeout * 3)
            if 'r' not in box:
                raise TimeoutError('parts server did not answer')
            out = box['r']
        else:
            out = asyncio.run(_call_async(tool, args, timeout))
    except Exception as e:                           # network, DNS, proxy, server errors
        raise PartsUnavailable(f'parts lookup unavailable ({ENDPOINT}): {type(e).__name__}: {e}') from None
    if isinstance(out, dict):
        out.setdefault('source', CREDIT)
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def part(lcsc=None, mpn=None):
    return call('jlc_get_part', {'lcsc': lcsc, 'mpn': mpn})


def search(query, limit=10, **filters):
    return call('jlc_search', {'query': query, 'limit': limit, **filters})


def alternatives(lcsc, limit=10, **filters):
    return call('jlc_find_alternatives', {'lcsc': lcsc, 'limit': limit, **filters})


def pinout(lcsc):
    return call('jlc_get_pinout', {'lcsc': lcsc})
