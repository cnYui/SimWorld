"""In-app AI chat.

Two backends, both driving the same emcosim tools the MCP server exposes:

* claude_code (default): runs the official Claude Code CLI installed on this
  computer, the way ChatCut Desktop does. The user signs in through Claude
  Code's own login (`claude auth login`), the app never sees or stores Claude
  credentials, and usage counts against the user's own Claude plan. Each message
  is one `claude -p` call with stream-json output, resumed by session id, with
  the emcosim MCP server attached and Claude Code's built-in tools turned off.
* api_key: calls the Claude API directly with an Anthropic API key the user
  enters (for machines without Claude Code, or a copy shared with other people).

Anthropic's terms do not allow a third-party app to offer claude.ai login or
to route requests through a Pro/Max subscription itself, which is why the app
delegates sign-in to the unmodified Claude Code binary instead of doing OAuth.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

SYSTEM_PROMPT = """You are the assistant inside the emcosim desktop app, an electronics + physics
co-simulation tool for ESP32 devices (firmware in QEMU, power and motor equivalent models,
MuJoCo mechanics, 1 ms lock-step). The user sees the project, parameters, scenarios and run
results in panels next to this chat; runs you start with run_scenario or sweep appear there
automatically, so mention the run_id instead of pasting long data. Use the emcosim tools to
inspect the project, change parameters (set_param writes project.yaml), write scenarios, run
and compare. Parameter values marked as assumptions in project.yaml are not measurements: say
so when a conclusion depends on them. Reply in the user's language, briefly."""

WIN_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# --------------------------------------------------------------------------- helpers

def find_claude(override: str | None = None):
    if override and Path(override).exists():
        return override
    for name in ('claude', 'claude.exe', 'claude.cmd'):
        p = shutil.which(name)
        if p:
            return p
    if os.name == 'nt':
        home = Path.home()
        for cand in (home / '.local' / 'bin' / 'claude.exe',
                     Path(os.environ.get('APPDATA', '')) / 'npm' / 'claude.cmd'):
            if cand.exists():
                return str(cand)
    return None


def emcosim_command(project_root):
    """Command line that starts this installation's MCP server."""
    if getattr(sys, 'frozen', False):
        exe = Path(sys.executable)
        cli = exe.with_name('emcosim.exe' if os.name == 'nt' else 'emcosim')
        return str(cli if cli.exists() else exe), ['--project', str(project_root), 'mcp']
    return sys.executable, ['-m', 'emcosim', '--project', str(project_root), 'mcp']


def _summarize_result(content, limit=600):
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get('type') == 'text':
                parts.append(c.get('text', ''))
            elif isinstance(c, dict) and c.get('type') == 'image':
                parts.append('[image]')
        content = '\n'.join(parts)
    text = str(content or '')
    return text if len(text) <= limit else text[:limit] + ' …'


class Conversation:
    def __init__(self, cid=None):
        self.id = cid or uuid.uuid4().hex[:12]
        self.items = []              # what the UI shows
        self.session_id = None       # Claude Code session
        self.api_messages = []       # API-key backend history
        self.busy = False
        self.proc = None
        self.cancel = threading.Event()
        self.created = time.time()

    def public(self):
        return {'id': self.id, 'items': self.items, 'busy': self.busy, 'created': self.created,
                'title': next((i['text'][:40] for i in self.items if i['role'] == 'user'), '新对话')}


# --------------------------------------------------------------------------- manager

class ChatManager:
    def __init__(self, bus, settings, get_root):
        self.bus, self.settings, self.get_root = bus, settings, get_root
        self.convs: dict[str, Conversation] = {}
        self._login_proc = None

    # -- status / auth ---------------------------------------------------------
    def status(self):
        s = self.settings.public()
        exe = find_claude(s.get('claude_path'))
        cc = {'found': bool(exe), 'path': exe, 'logged_in': False, 'version': None, 'auth_method': None,
              'login_running': bool(self._login_proc and self._login_proc.poll() is None)}
        if exe:
            try:
                v = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=20,
                                   creationflags=WIN_FLAGS)
                cc['version'] = (v.stdout or '').strip().split('\n')[0] or None
                r = subprocess.run([exe, 'auth', 'status', '--json'], capture_output=True, text=True,
                                   timeout=20, creationflags=WIN_FLAGS)
                info = json.loads(r.stdout or '{}')
                cc['logged_in'] = bool(info.get('loggedIn'))
                cc['auth_method'] = info.get('authMethod')
            except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                cc['error'] = str(e)
        return {'backend': s.get('chat_backend', 'claude_code'), 'claude_code': cc,
                'api_key': {'configured': bool(self.settings.api_key()), 'model': s.get('model')}}

    def login(self, console=False):
        """Start Claude Code's own sign-in (opens the browser). On Windows it runs in
        its own console window so the user can paste a code if the browser hand-off
        fails; elsewhere its output (including the sign-in URL) streams to the UI."""
        exe = find_claude(self.settings.data.get('claude_path'))
        if not exe:
            raise RuntimeError('没有找到 Claude Code。请先安装：https://claude.com/claude-code')
        if self._login_proc and self._login_proc.poll() is None:
            return {'started': False, 'running': True}
        cmd = [exe, 'auth', 'login', '--claudeai'] if not console else [exe, 'auth', 'login', '--console']
        if os.name == 'nt':
            self._login_proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            self._login_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, text=True)
            threading.Thread(target=self._pump_login, daemon=True).start()
        threading.Thread(target=self._wait_login, daemon=True).start()
        return {'started': True}

    def _pump_login(self):
        for line in self._login_proc.stdout:
            self.bus.publish('auth_log', {'line': line.rstrip()})

    def _wait_login(self):
        self._login_proc.wait()
        self.bus.publish('auth_changed', {'exit_code': self._login_proc.returncode})

    def logout(self):
        exe = find_claude(self.settings.data.get('claude_path'))
        if exe:
            subprocess.run([exe, 'auth', 'logout'], capture_output=True, timeout=30, creationflags=WIN_FLAGS)
        self.bus.publish('auth_changed', {})
        return {'ok': True}

    # -- conversations -----------------------------------------------------------
    def new(self):
        c = Conversation()
        self.convs[c.id] = c
        return c.public()

    def list(self):
        return [c.public() for c in sorted(self.convs.values(), key=lambda c: c.created, reverse=True)]

    def get(self, cid):
        return self.convs[cid].public()

    def stop(self, cid):
        c = self.convs[cid]
        c.cancel.set()
        if c.proc and c.proc.poll() is None:
            c.proc.terminate()
        return {'ok': True}

    def send(self, cid, text):
        c = self.convs.get(cid) or self.convs.setdefault(cid, Conversation(cid))
        if c.busy:
            raise RuntimeError('上一条消息还在处理中')
        if self.get_root() is None:
            raise RuntimeError('先打开一个工程')
        c.busy = True
        c.cancel.clear()
        c.items.append({'role': 'user', 'text': text, 'time': time.time()})
        self._emit(c, 'user', {'text': text})
        backend = self.settings.data.get('chat_backend', 'claude_code')
        target = self._run_claude_code if backend == 'claude_code' else self._run_api
        threading.Thread(target=self._guard, args=(target, c, text), daemon=True).start()
        return {'conversation': c.id, 'backend': backend}

    def _guard(self, fn, c, text):
        try:
            fn(c, text)
        except Exception as e:      # shown in the chat instead of failing silently
            c.items.append({'role': 'error', 'text': f'{type(e).__name__}: {e}'})
            self._emit(c, 'error', {'text': f'{type(e).__name__}: {e}'})
        finally:
            c.busy = False
            c.proc = None
            self._emit(c, 'done', {})

    def _emit(self, c, type_, data):
        self.bus.publish('chat', {'conversation': c.id, 'type': type_, **data})

    # -- assistant transcript helpers ------------------------------------------
    def _text_delta(self, c, text):
        last = c.items[-1] if c.items else None
        if not last or last['role'] != 'assistant' or last.get('closed'):
            last = {'role': 'assistant', 'text': ''}
            c.items.append(last)
        last['text'] += text
        self._emit(c, 'delta', {'text': text})

    def _close_text(self, c):
        if c.items and c.items[-1]['role'] == 'assistant':
            c.items[-1]['closed'] = True

    def _tool_use(self, c, tid, name, inp):
        self._close_text(c)
        short = name.split('__')[-1]
        c.items.append({'role': 'tool', 'id': tid, 'name': short, 'input': inp, 'status': 'running'})
        self._emit(c, 'tool_use', {'id': tid, 'name': short, 'input': inp})

    def _tool_result(self, c, tid, ok, summary):
        for it in reversed(c.items):
            if it.get('role') == 'tool' and it.get('id') == tid:
                it['status'] = 'ok' if ok else 'error'
                it['result'] = summary
                break
        self._emit(c, 'tool_result', {'id': tid, 'ok': ok, 'result': summary})

    # -- backend 1: local Claude Code ------------------------------------------
    def _run_claude_code(self, c, text):
        exe = find_claude(self.settings.data.get('claude_path'))
        if not exe:
            raise RuntimeError('没有找到 Claude Code。安装后在设置里点“登录 Claude”，或者改用 API Key。')
        root = Path(self.get_root())
        cmd_exe, cmd_args = emcosim_command(root)
        cfg = {'mcpServers': {'emcosim': {'command': cmd_exe, 'args': cmd_args}}}
        tmp = Path(tempfile.gettempdir()) / f'emcosim-mcp-{os.getpid()}.json'
        tmp.write_text(json.dumps(cfg), encoding='utf8')
        cmd = [exe, '-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
               '--mcp-config', str(tmp), '--strict-mcp-config', '--tools', '',
               '--allowedTools', 'mcp__emcosim', '--append-system-prompt', SYSTEM_PROMPT]
        if c.session_id:
            cmd += ['--resume', c.session_id]
        else:
            c.session_id = str(uuid.uuid4())
            cmd += ['--session-id', c.session_id]
        env = dict(os.environ)
        env.pop('ANTHROPIC_API_KEY', None)      # use Claude Code's own sign-in
        c.proc = subprocess.Popen(cmd, cwd=str(root), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding='utf8', errors='replace',
                                  creationflags=WIN_FLAGS)
        c.proc.stdin.write(text)
        c.proc.stdin.close()
        err_lines = []
        threading.Thread(target=lambda: err_lines.extend(c.proc.stderr), daemon=True).start()
        streamed = False
        result = None
        for line in c.proc.stdout:
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get('type')
            if t == 'system' and ev.get('subtype') == 'init':
                bad = [s for s in ev.get('mcp_servers', []) if s.get('status') != 'connected']
                if bad:
                    self._emit(c, 'notice', {'text': f'emcosim 工具没有连上：{bad}'})
            elif t == 'stream_event':
                e = ev.get('event', {})
                if e.get('type') == 'content_block_delta' and e.get('delta', {}).get('type') == 'text_delta':
                    streamed = True
                    self._text_delta(c, e['delta']['text'])
                elif e.get('type') == 'message_start':
                    self._close_text(c)
            elif t == 'assistant':
                for b in ev.get('message', {}).get('content', []):
                    if b.get('type') == 'text' and not streamed:
                        self._text_delta(c, b.get('text', ''))
                    elif b.get('type') == 'tool_use':
                        self._tool_use(c, b['id'], b['name'], b.get('input'))
                streamed = False
            elif t == 'user':
                content = ev.get('message', {}).get('content', [])
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get('type') == 'tool_result':
                            self._tool_result(c, b.get('tool_use_id'), not b.get('is_error'),
                                              _summarize_result(b.get('content')))
            elif t == 'result':
                result = ev
        c.proc.wait()
        self._close_text(c)
        if c.cancel.is_set():
            c.items.append({'role': 'notice', 'text': '已停止'})
            self._emit(c, 'notice', {'text': '已停止'})
            return
        if result is None or result.get('is_error'):
            msg = (result or {}).get('result') or ''.join(err_lines[-20:]).strip() or f'exit code {c.proc.returncode}'
            if 'login' in msg.lower() or 'auth' in msg.lower():
                msg += '\n（Claude Code 还没登录：在设置里点“登录 Claude”。）'
            raise RuntimeError(msg)
        meta = {'cost_usd': result.get('total_cost_usd'), 'duration_ms': result.get('duration_ms'),
                'turns': result.get('num_turns')}
        c.items.append({'role': 'meta', **meta})
        self._emit(c, 'meta', meta)

    # -- backend 2: Claude API with an API key ---------------------------------
    def _run_api(self, c, text):
        import anthropic
        from ..mcp_server import build_server

        key = self.settings.api_key()
        if not key:
            raise RuntimeError('还没有设置 API Key（设置 → AI）')
        client = anthropic.Anthropic(api_key=key)
        server = build_server(self.get_root())
        loop = asyncio.new_event_loop()
        try:
            tools = []
            for t in loop.run_until_complete(server.list_tools()):
                schema = getattr(t, 'input_schema', None) or getattr(t, 'inputSchema', None) or {'type': 'object'}
                tools.append({'name': t.name, 'description': t.description or '', 'input_schema': schema})
            c.api_messages.append({'role': 'user', 'content': text})
            model = self.settings.data.get('model') or 'claude-opus-5-5'
            effort = self.settings.data.get('effort') or 'medium'
            while not c.cancel.is_set():
                with client.beta.messages.stream(
                        model=model, max_tokens=32000, system=SYSTEM_PROMPT, tools=tools,
                        messages=c.api_messages, thinking={'type': 'adaptive'},
                        output_config={'effort': effort},
                        betas=['server-side-fallback-2026-07-01'], fallbacks='default') as stream:
                    for event in stream:
                        if c.cancel.is_set():
                            break
                        if event.type == 'content_block_delta' and event.delta.type == 'text_delta':
                            self._text_delta(c, event.delta.text)
                        elif event.type == 'content_block_start' and event.content_block.type == 'text':
                            self._close_text(c)
                    if c.cancel.is_set():
                        break
                    msg = stream.get_final_message()
                self._close_text(c)
                c.api_messages.append({'role': 'assistant', 'content': msg.content})
                if msg.stop_reason == 'pause_turn':
                    continue
                uses = [b for b in msg.content if b.type == 'tool_use']
                if msg.stop_reason == 'refusal':
                    raise RuntimeError('模型拒绝了这个请求')
                if not uses:
                    break
                results = []
                for b in uses:
                    self._tool_use(c, b.id, b.name, b.input)
                    if not isinstance(b.input, dict):
                        res, ok = [{'type': 'text', 'text': 'invalid tool input'}], False
                    else:
                        res, ok = self._call_tool(loop, server, b.name, b.input)
                    self._tool_result(c, b.id, ok, _summarize_result(res))
                    results.append({'type': 'tool_result', 'tool_use_id': b.id, 'content': res,
                                    **({} if ok else {'is_error': True})})
                c.api_messages.append({'role': 'user', 'content': results})
            usage = getattr(msg, 'usage', None) if 'msg' in locals() else None
            meta = {'input_tokens': getattr(usage, 'input_tokens', None),
                    'output_tokens': getattr(usage, 'output_tokens', None), 'model': model}
            c.items.append({'role': 'meta', **meta})
            self._emit(c, 'meta', meta)
        finally:
            loop.close()

    @staticmethod
    def _call_tool(loop, server, name, args):
        try:
            r = loop.run_until_complete(server.call_tool(name, args))
        except Exception as e:
            return [{'type': 'text', 'text': f'{type(e).__name__}: {e}'}], False
        is_error = bool(getattr(r, 'is_error', False) or getattr(r, 'isError', False))
        items = getattr(r, 'content', r)
        if isinstance(items, tuple):            # mcp 1.x: (content, structured)
            items = items[0]
        out = []
        for it in items or []:
            kind = getattr(it, 'type', None)
            if kind == 'text':
                out.append({'type': 'text', 'text': it.text})
            elif kind == 'image':
                data = it.data if isinstance(it.data, str) else base64.b64encode(it.data).decode()
                out.append({'type': 'image', 'source': {'type': 'base64', 'media_type': it.mimeType
                            if hasattr(it, 'mimeType') else it.mime_type, 'data': data}})
        return out or [{'type': 'text', 'text': '(empty)'}], not is_error
