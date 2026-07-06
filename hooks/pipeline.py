from __future__ import annotations

import json
import time

from core.config import ACTION_LOG_PATH, WORKDIR, print_event
from tools.file_ops import safe_path

HOOKS = {'UserPromptSubmit': [], 'PreToolUse': [], 'PostToolUse': [], 'Stop': []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


DENY_LIST = ['rm -rf /', 'sudo', 'shutdown', 'reboot', 'mkfs', 'dd if=']
DESTRUCTIVE = ['rm ', '> /etc/', 'chmod 777']
CONFIRM_KEYWORDS = ['git push', 'git branch -D', 'pip install', 'npm install', 'cargo build', 'pytest', 'docker']
MCP_WRITE_KEYWORDS = ['deploy', 'trigger', 'delete', 'update', 'create']


def summarize_tool_input(block) -> dict:
    raw = dict(block.input or {})
    summary = {}
    for key, value in raw.items():
        text = str(value)
        summary[key] = text if len(text) <= 160 else text[:157] + '...'
    return summary


def classify_tool_risk(block) -> str:
    if block.name == 'bash':
        command = block.input.get('command', '')
        if any(pattern in command for pattern in DENY_LIST):
            return 'deny'
        if any(token in command for token in DESTRUCTIVE):
            return 'deny'
        if any(token in command for token in CONFIRM_KEYWORDS):
            return 'confirm'
        return 'safe'
    if block.name in ('write_file', 'edit_file', 'create_worktree', 'remove_worktree'):
        return 'confirm'
    if block.name.startswith('mcp__'):
        try:
            from mcp.manager import get_mcp_tool_metadata

            metadata = get_mcp_tool_metadata(block.name)
            if metadata.get('risk') in ('deny', 'confirm', 'safe'):
                return metadata['risk']
        except Exception:
            pass
        lowered = block.name.lower()
        if any(token in lowered for token in MCP_WRITE_KEYWORDS):
            return 'confirm'
        return 'safe'
    return 'safe'


def audit_event(event_type: str, payload: dict):
    record = {'ts': time.time(), 'event': event_type, **payload}
    with ACTION_LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=True) + '\n')


def permission_hook(block):
    risk = classify_tool_risk(block)
    audit_event(
        'tool_pre',
        {
            'tool': block.name,
            'risk': risk,
            'input': summarize_tool_input(block),
        },
    )
    if block.name == 'bash':
        command = block.input.get('command', '')
        for pattern in DENY_LIST:
            if pattern in command:
                audit_event('tool_blocked', {'tool': block.name, 'reason': f'deny_list:{pattern}'})
                return f"Permission denied: '{pattern}' is on the deny list"
        if risk == 'confirm':
            print()
            print_event('confirm', 'Destructive shell command requires approval.', 179)
            print(f'  {command}')
            choice = input('  Allow? [y/N] ').strip().lower()
            if choice not in ('y', 'yes'):
                audit_event('tool_blocked', {'tool': block.name, 'reason': 'user_denied'})
                return 'Permission denied by user'
    if block.name in ('write_file', 'edit_file', 'create_worktree', 'remove_worktree') and risk == 'confirm':
        print()
        print_event('confirm', f'{block.name} requires approval.', 179)
        for key, value in summarize_tool_input(block).items():
            print(f'  {key}: {value}')
        choice = input('  Allow? [y/N] ').strip().lower()
        if choice not in ('y', 'yes'):
            audit_event('tool_blocked', {'tool': block.name, 'reason': 'user_denied'})
            return 'Permission denied by user'
    if block.name in ('write_file', 'edit_file'):
        path = block.input.get('path', '')
        try:
            safe_path(path)
        except Exception:
            audit_event('tool_blocked', {'tool': block.name, 'reason': f'path_escape:{path}'})
            return f'Permission denied: path escapes workspace: {path}'
    if block.name.startswith('mcp__') and risk == 'confirm':
        print()
        print_event('confirm', f'MCP write-capable tool: {block.name}', 179)
        choice = input('  Allow? [y/N] ').strip().lower()
        if choice not in ('y', 'yes'):
            audit_event('tool_blocked', {'tool': block.name, 'reason': 'user_denied'})
            return 'Permission denied by user'
    return None


def log_hook(block):
    print_event('tool', block.name, 81)
    return None


def large_output_hook(block, output):
    audit_event(
        'tool_post',
        {
            'tool': block.name,
            'output_preview': str(output)[:200],
            'output_chars': len(str(output)),
        },
    )
    if len(str(output)) > 100000:
        print_event('warn', f'Large output from {block.name}: {len(str(output))} chars', 179)
    return None


def user_prompt_hook(query: str):
    print_event('user', str(WORKDIR), 75)
    return None


def stop_hook(messages: list):
    tool_count = 0
    for msg in messages:
        content = msg.get('content')
        if isinstance(content, list):
            tool_count += sum(1 for item in content if isinstance(item, dict) and item.get('type') == 'tool_result')
    print_event('stop', f'{tool_count} tool result(s)', 244)
    return None


register_hook('UserPromptSubmit', user_prompt_hook)
register_hook('PreToolUse', permission_hook)
register_hook('PreToolUse', log_hook)
register_hook('PostToolUse', large_output_hook)
register_hook('Stop', stop_hook)
