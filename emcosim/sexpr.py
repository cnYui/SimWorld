"""Minimal S-expression reader/writer for KiCad files."""
import re

_TOKEN = re.compile(r'\s*(?:(\()|(\))|("(?:[^"\\]|\\.)*")|([^\s()"]+))', re.S)


class Sym(str):
    """Bare (unquoted) atom."""


def parse(text):
    stack, cur = [], []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == '':
                break
            raise ValueError(f'bad s-expression near offset {pos}')
        pos = m.end()
        lp, rp, qs, atom = m.groups()
        if lp:
            stack.append(cur)
            cur = []
        elif rp:
            done = cur
            cur = stack.pop()
            cur.append(done)
        elif qs is not None:
            cur.append(bytes(qs[1:-1], 'utf8').decode('unicode_escape') if '\\' in qs else qs[1:-1])
        elif atom is not None:
            cur.append(Sym(atom))
    if stack:
        raise ValueError('unbalanced parentheses')
    return cur[0] if len(cur) == 1 else cur


def head(node):
    return node[0] if isinstance(node, list) and node else None


def children(node, name):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def child(node, name):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def dumps(node, indent=0):
    if not isinstance(node, list):
        if isinstance(node, Sym):
            return str(node)
        if isinstance(node, (int, float)):
            return f'{node:.6g}' if isinstance(node, float) else str(node)
        return '"' + str(node).replace('\\', '\\\\').replace('"', '\\"') + '"'
    simple = all(not isinstance(c, list) for c in node)
    if simple:
        return '(' + ' '.join(dumps(c) for c in node) + ')'
    pad = '  ' * (indent + 1)
    parts = [dumps(node[0])]
    out = '(' + parts[0]
    for c in node[1:]:
        if isinstance(c, list):
            out += '\n' + pad + dumps(c, indent + 1)
        else:
            out += ' ' + dumps(c)
    return out + ')'
