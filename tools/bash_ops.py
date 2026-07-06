from __future__ import annotations

import subprocess
import threading

from core.config import WORKDIR
from hooks.pipeline import trigger_hooks

_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def run_bash(command: str, cwd=None, run_in_background: bool = False) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=cwd or WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else '(no output)'
    except subprocess.TimeoutExpired:
        return 'Error: Timeout (120s)'


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    if tool_name != 'bash':
        return False
    command = tool_input.get('command', '').lower()
    slow_keywords = ['install', 'build', 'test', 'deploy', 'compile', 'docker build', 'pip install', 'npm install', 'cargo build', 'pytest', 'make']
    return any(keyword in command for keyword in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    if tool_name != 'bash':
        return False
    return bool(tool_input.get('run_in_background')) or is_slow_operation(tool_name, tool_input)


def start_background_task(block, handlers: dict) -> str:
    global _bg_counter
    with background_lock:
        _bg_counter += 1
        bg_id = f'bg_{_bg_counter:04d}'
    command = block.input.get('command', block.name)

    def worker():
        from tools.builtin import call_tool_handler

        handler = handlers.get(block.name)
        result = call_tool_handler(handler, block.input, block.name)
        trigger_hooks('PostToolUse', block, result)
        with background_lock:
            background_tasks[bg_id]['status'] = 'completed'
            background_results[bg_id] = str(result)

    with background_lock:
        background_tasks[bg_id] = {'tool_use_id': block.id, 'command': command, 'status': 'running'}
    threading.Thread(target=worker, daemon=True).start()
    print(f'  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m')
    return bg_id


def collect_background_results() -> list[str]:
    with background_lock:
        ready = [bg_id for bg_id, task in background_tasks.items() if task['status'] == 'completed']
    notifications = []
    for bg_id in ready:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, '')
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f'<task_notification>\n'
            f'  <task_id>{bg_id}</task_id>\n'
            f'  <status>completed</status>\n'
            f"  <command>{task['command']}</command>\n"
            f'  <summary>{summary}</summary>\n'
            f'</task_notification>'
        )
    return notifications
