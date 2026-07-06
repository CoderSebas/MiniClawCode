from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass

from core.config import MCP_CONFIG_PATH, WORKDIR, print_event

mcp_clients: dict[str, 'MCPClient'] = {}
mcp_activity: dict[str, dict] = {}
mcp_tool_meta: dict[str, dict] = {}
_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


@dataclass
class MCPServerDefinition:
    name: str
    factory: callable
    mode: str = 'read_only'
    description: str = ''


class MCPClient:
    """Discovers and calls tools on an MCP server (mock for teaching)."""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def register(self, tool_defs: list[dict], handlers: dict[str, callable]):
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        activity = mcp_activity.setdefault(
            self.name,
            {'connected_at': time.time(), 'call_count': 0, 'last_tool': '', 'last_result_preview': '', 'tools': []},
        )
        activity['call_count'] += 1
        activity['last_tool'] = tool_name
        handler = self._handlers.get(tool_name)
        if not handler:
            result = f"MCP error: unknown tool '{tool_name}'"
            activity['last_result_preview'] = result[:200]
            return result
        try:
            result = handler(**args)
            activity['last_result_preview'] = str(result)[:200]
            return result
        except Exception as e:
            result = f'MCP error: {e}'
            activity['last_result_preview'] = result[:200]
            return result


SERVER_REGISTRY: dict[str, MCPServerDefinition] = {}


def normalize_mcp_name(name: str) -> str:
    return _DISALLOWED_CHARS.sub('_', name)


def register_mcp_server(name: str, factory, mode: str = 'read_only', description: str = ''):
    SERVER_REGISTRY[name] = MCPServerDefinition(name=name, factory=factory, mode=mode, description=description)


def list_registered_servers() -> str:
    if not SERVER_REGISTRY:
        return 'No MCP servers registered.'
    return '\n'.join(
        f"  {server.name}: [{server.mode}] {server.description or 'No description'}"
        for server in SERVER_REGISTRY.values()
    )


def _build_shell_tool_handler(command_template: str, cwd: str | None = None, timeout: int = 30):
    def _handler(**args):
        command = command_template.format_map({key: str(value) for key, value in args.items()})
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd or WORKDIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        return output[:50000] if output else '(no output)'

    return _handler


def _config_server_factory(server_def: dict):
    def _factory():
        client = MCPClient(server_def['name'])
        tool_defs = []
        handlers = {}
        for tool in server_def.get('tools', []):
            tool_defs.append(
                {
                    'name': tool['name'],
                    'description': tool.get('description', ''),
                    'inputSchema': tool.get('inputSchema', {'type': 'object', 'properties': {}, 'required': []}),
                    'risk': tool.get('risk', 'safe'),
                }
            )
            handlers[tool['name']] = _build_shell_tool_handler(
                tool.get('command', ''),
                cwd=tool.get('cwd'),
                timeout=int(tool.get('timeout', 30)),
            )
        client.register(tool_defs=tool_defs, handlers=handlers)
        return client

    return _factory


def load_configured_servers():
    if not MCP_CONFIG_PATH.exists():
        return
    try:
        payload = json.loads(MCP_CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception as e:
        print_event('warn', f'Failed to load MCP config: {e}', 179)
        return
    for server_def in payload.get('servers', []):
        name = server_def.get('name', '').strip()
        if not name:
            continue
        register_mcp_server(
            name,
            _config_server_factory(server_def),
            mode=server_def.get('mode', 'read_only'),
            description=server_def.get('description', 'Configured MCP server'),
        )


def _mock_server_docs():
    client = MCPClient('docs')
    client.register(
        tool_defs=[
            {'name': 'search', 'description': 'Search documentation. (readOnly)', 'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}},
            {'name': 'get_version', 'description': 'Get API version. (readOnly)', 'inputSchema': {'type': 'object', 'properties': {}, 'required': []}},
        ],
        handlers={
            'search': lambda query: f"[docs] Found 3 results for '{query}'",
            'get_version': lambda: '[docs] API v2.1.0',
        },
    )
    return client


def _mock_server_deploy():
    client = MCPClient('deploy')
    client.register(
        tool_defs=[
            {'name': 'trigger', 'description': 'Trigger a deployment. (destructive - requires approval in real CC)', 'inputSchema': {'type': 'object', 'properties': {'service': {'type': 'string'}}, 'required': ['service']}},
            {'name': 'status', 'description': 'Check deployment status. (readOnly)', 'inputSchema': {'type': 'object', 'properties': {'service': {'type': 'string'}}, 'required': ['service']}},
        ],
        handlers={
            'trigger': lambda service: f'[deploy] Triggered: {service}',
            'status': lambda service: f'[deploy] {service}: running (v1.4.2)',
        },
    )
    return client


register_mcp_server('docs', _mock_server_docs, mode='read_only', description='Documentation search and version lookup')
register_mcp_server('deploy', _mock_server_deploy, mode='write_capable', description='Deployment status and trigger operations')
load_configured_servers()


def connect_mcp(name: str) -> str:
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    definition = SERVER_REGISTRY.get(name)
    if not definition:
        available = ', '.join(SERVER_REGISTRY.keys())
        return f"Unknown server '{name}'. Available: {available}"
    mcp_client = definition.factory()
    mcp_clients[name] = mcp_client
    tool_names = [t['name'] for t in mcp_client.tools]
    mcp_activity[name] = {
        'connected_at': time.time(),
        'call_count': 0,
        'last_tool': '',
        'last_result_preview': '',
        'mode': definition.mode,
        'description': definition.description,
        'tools': tool_names,
    }
    print_event('mcp', f'connected {name} ({definition.mode}) -> {tool_names}', 141)
    return f"Connected to MCP server '{name}'. Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}"


def get_mcp_tool_metadata(prefixed_name: str) -> dict:
    return mcp_tool_meta.get(prefixed_name, {})


def list_connected_servers() -> str:
    if not mcp_clients:
        return 'No MCP servers connected.'
    lines = []
    for name, client in mcp_clients.items():
        activity = mcp_activity.get(name, {})
        lines.append(
            f"  {name}: tools={len(client.tools)} calls={activity.get('call_count', 0)}"
            + (f" last={activity.get('last_tool')}" if activity.get('last_tool') else '')
        )
    return '\n'.join(lines)


def list_mcp_tools() -> str:
    if not mcp_clients:
        return 'No MCP tools available.'
    lines = []
    for name, client in mcp_clients.items():
        lines.append(f'[{name}]')
        for tool in client.tools:
            lines.append(f"  {tool['name']}: {tool.get('description', '')}")
    return '\n'.join(lines)


def assemble_tool_pool() -> tuple[list[dict], dict]:
    from tools.builtin import BUILTIN_HANDLERS, BUILTIN_TOOLS

    tools = list(BUILTIN_TOOLS)
    handlers = dict(BUILTIN_HANDLERS)
    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            safe_tool = normalize_mcp_name(tool_def['name'])
            prefixed = f'mcp__{safe_server}__{safe_tool}'
            tools.append({'name': prefixed, 'description': tool_def.get('description', ''), 'input_schema': tool_def.get('inputSchema', {})})
            mcp_tool_meta[prefixed] = {
                'server': server_name,
                'tool': tool_def['name'],
                'mode': mcp_activity.get(server_name, {}).get('mode', 'read_only'),
                'risk': tool_def.get('risk', 'confirm' if mcp_activity.get(server_name, {}).get('mode') == 'write_capable' else 'safe'),
            }
            handlers[prefixed] = lambda *, c=mcp_client, t=tool_def['name'], **kw: c.call_tool(t, kw)
    return tools, handlers
