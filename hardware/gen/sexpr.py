"""Minimal S-expression reader/writer for KiCad files."""
import re

_tok = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()"]+')

class Sym(str):
    """Bare (unquoted) atom."""

def parse(text):
    stack, cur = [], []
    for t in _tok.findall(text):
        if t == '(':
            stack.append(cur); cur = []
        elif t == ')':
            done = cur; cur = stack.pop(); cur.append(done)
        elif t[0] == '"':
            cur.append(t[1:-1].replace('\\"', '"').replace('\\\\', '\\'))
        else:
            cur.append(Sym(t))
    return cur[0]

def dump(node, ind=0):
    if isinstance(node, list):
        flat = '(' + ' '.join(dump(n) for n in node) + ')'
        if len(flat) < 100 or not any(isinstance(n, list) for n in node):
            return flat
        pad = '\n' + '  ' * (ind + 1)
        head = [dump(n) for n in node if not isinstance(n, list)]
        return '(' + ' '.join(head) + ''.join(pad + dump(n, ind + 1) for n in node if isinstance(n, list)) + '\n' + '  ' * ind + ')'
    if isinstance(node, Sym):
        return str(node)
    if isinstance(node, (int, float)):
        return ('%.4f' % node).rstrip('0').rstrip('.') if isinstance(node, float) else str(node)
    return '"' + str(node).replace('\\', '\\\\').replace('"', '\\"') + '"'

def find(node, key):
    return [n for n in node if isinstance(n, list) and n and n[0] == key]

def first(node, key):
    r = find(node, key)
    return r[0] if r else None
