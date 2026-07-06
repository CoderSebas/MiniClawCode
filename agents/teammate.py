from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from agents.protocol import new_request_id
from agents.subagent import has_tool_use
from bus.message_bus import BUS
from context.memory_store import remember
from core.client import client
from core.config import MODEL, TEAMMATE_STALE_SECONDS, WORKTREES_DIR
import core.state as core_state
from models.schemas import ProtocolState
from tools.bash_ops import run_bash
from tools.builtin import call_tool_handler
from tools.file_ops import run_read, run_write
from tools.task_tools import claim_task, complete_task, list_tasks, load_task, requeue_task, scan_unclaimed_tasks

IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60
ROLE_REGISTRY = {
    'planner': {
        'prompt': 'You are a planning specialist. Break work down, sequence tasks, and ask for approval before implementation. Do not modify files.',
        'allowed_tools': {'read_file', 'send_message', 'submit_plan', 'list_tasks', 'claim_task', 'complete_task'},
        'auto_claim': True,
    },
    'coder': {
        'prompt': 'You are an implementation specialist. Make focused code changes, respect the current task, and report concise outcomes.',
        'allowed_tools': {'bash', 'read_file', 'write_file', 'send_message', 'submit_plan', 'list_tasks', 'claim_task', 'complete_task'},
        'auto_claim': True,
    },
    'reviewer': {
        'prompt': 'You are a code reviewer. Inspect code and changes for bugs, regressions, and missing tests. Do not modify files.',
        'allowed_tools': {'read_file', 'send_message', 'submit_plan', 'list_tasks', 'claim_task', 'complete_task'},
        'auto_claim': True,
    },
    'tester': {
        'prompt': 'You are a testing specialist. Run safe read-only checks and summarize failures or regressions. Avoid file edits and avoid mutating shell commands.',
        'allowed_tools': {'read_file', 'send_message', 'submit_plan', 'list_tasks', 'claim_task', 'complete_task'},
        'auto_claim': True,
    },
}


def _resolve_worktree_path(worktree_name: str | None) -> str | None:
    if not worktree_name:
        return None
    wt_path = WORKTREES_DIR / worktree_name
    if wt_path.exists() and wt_path.is_dir():
        return str(wt_path)
    return None


def _update_teammate_state(name: str, **changes):
    state = core_state.active_teammates.get(name, {})
    if not isinstance(state, dict):
        state = {'status': 'running'}
    state.update(changes)
    state['updated_at'] = time.time()
    core_state.active_teammates[name] = state


def reap_stale_teammates(max_idle_seconds: int = TEAMMATE_STALE_SECONDS) -> list[str]:
    now = time.time()
    notifications = []
    for name, state in list(core_state.active_teammates.items()):
        if not isinstance(state, dict):
            continue
        updated_at = state.get('updated_at', 0)
        if not updated_at or now - updated_at < max_idle_seconds:
            continue
        task_id = state.get('task_id', '')
        if task_id:
            try:
                requeue_task(task_id, reason=f'teammate {name} became stale after {max_idle_seconds}s')
            except FileNotFoundError:
                pass
        remember(
            f'teammate-{name}-stale',
            f'Teammate marked stale: {name}',
            f"Teammate '{name}' was marked stale after {int(now - updated_at)} seconds without heartbeat.",
            note_type='event',
        )
        notifications.append(f"Teammate '{name}' marked stale and removed from active roster.")
        core_state.active_teammates.pop(name, None)
    return notifications


def idle_poll(agent_name: str, messages: list, name: str, role: str, worktree_context: dict | None = None) -> str:
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        _update_teammate_state(name, status='idle', last_event='idle_poll')
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get('type') == 'shutdown_request':
                    req_id = msg.get('metadata', {}).get('request_id', '')
                    BUS.send(name, 'lead', 'Shutting down.', 'shutdown_response', {'request_id': req_id, 'approve': True})
                    return 'shutdown'
            messages.append({'role': 'user', 'content': '<inbox>' + json.dumps(inbox) + '</inbox>'})
            return 'work'
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data['id'], agent_name)
            if 'Claimed' in result:
                wt_info = ''
                if task_data.get('worktree'):
                    wt_path = _resolve_worktree_path(task_data['worktree'])
                    if wt_path:
                        wt_info = f'\nWork directory: {wt_path}'
                    if worktree_context is not None:
                        worktree_context['path'] = wt_path
                _update_teammate_state(name, task_id=task_data['id'], task_subject=task_data['subject'], status='working', last_event='auto_claimed')
                messages.append({'role': 'user', 'content': f"<auto-claimed>Task {task_data['id']}: {task_data['subject']}{wt_info}</auto-claimed>"})
                return 'work'
    return 'timeout'


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    req_id = new_request_id()
    core_state.pending_requests[req_id] = ProtocolState(
        request_id=req_id,
        type='plan_approval',
        sender=from_name,
        target='lead',
        status='pending',
        payload=plan,
    )
    BUS.send(from_name, 'lead', plan, 'plan_approval_request', {'request_id': req_id})
    return f'Plan submitted ({req_id})'


def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    if name in core_state.active_teammates:
        return f"Teammate '{name}' already exists"

    role_key = role if role in ROLE_REGISTRY else 'coder'
    role_cfg = ROLE_REGISTRY[role_key]
    protocol_ctx = {'waiting_plan': None}
    remember(
        f'teammate-{name}-spawned',
        f'Teammate spawned: {name}',
        f"Teammate '{name}' spawned with role '{role_key}'.\nPrompt: {prompt[:400]}",
        note_type='event',
    )
    system = (
        f"You are '{name}', a {role_key}. Use tools to complete tasks. "
        f"If a task has a worktree, work in that directory.\n"
        f"{role_cfg['prompt']}"
    )

    def handle_inbox_message(local_name: str, msg: dict, messages: list):
        msg_type = msg.get('type', 'message')
        meta = msg.get('metadata', {})
        req_id = meta.get('request_id', '')
        if msg_type == 'shutdown_request':
            BUS.send(local_name, 'lead', 'Shutting down.', 'shutdown_response', {'request_id': req_id, 'approve': True})
            _update_teammate_state(local_name, status='shutting_down', last_event='shutdown_request')
            return True
        if msg_type == 'plan_approval_response':
            approve = meta.get('approve', False)
            if req_id == protocol_ctx['waiting_plan']:
                protocol_ctx['waiting_plan'] = None
            _update_teammate_state(local_name, waiting_plan=None, last_event='plan_approved' if approve else 'plan_rejected')
            messages.append({'role': 'user', 'content': '[Plan approved]' if approve else f"[Plan rejected] {msg['content']}"})
        return False

    def run():
        wt_ctx = {'path': None}
        current_task_id = {'value': ''}

        def _wt_cwd():
            p = wt_ctx['path']
            return Path(p) if p else None

        def _run_bash(command: str) -> str:
            return run_bash(command, cwd=_wt_cwd())

        def _run_read(path: str) -> str:
            return run_read(path, cwd=_wt_cwd())

        def _run_write(path: str, content: str) -> str:
            return run_write(path, content, cwd=_wt_cwd())

        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return 'No tasks.'
            return '\n'.join(f'  {t.id}: {t.subject} [{t.status}]' + (f' (wt:{t.worktree})' if t.worktree else '') for t in tasks)

        def _run_claim_task(task_id: str):
            result = claim_task(task_id, owner=name)
            if 'Claimed' in result:
                task = load_task(task_id)
                current_task_id['value'] = task.id
                wt_ctx['path'] = _resolve_worktree_path(task.worktree)
                _update_teammate_state(name, task_id=task.id, task_subject=task.subject, worktree=task.worktree, status='working', last_event='claimed_task')
            return result

        def _run_complete_task(task_id: str):
            result = complete_task(task_id)
            current_task_id['value'] = ''
            _update_teammate_state(name, status='idle', task_id='', task_subject='', worktree='', last_event='completed_task')
            wt_ctx['path'] = None
            return result

        messages = [{'role': 'user', 'content': prompt}]
        all_sub_tools = [
            {'name': 'bash', 'description': 'Run a shell command.', 'input_schema': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']}},
            {'name': 'read_file', 'description': 'Read file.', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'limit': {'type': 'integer'}, 'offset': {'type': 'integer'}}, 'required': ['path']}},
            {'name': 'write_file', 'description': 'Write file.', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}}, 'required': ['path', 'content']}},
            {'name': 'send_message', 'description': 'Send message to another agent.', 'input_schema': {'type': 'object', 'properties': {'to': {'type': 'string'}, 'content': {'type': 'string'}}, 'required': ['to', 'content']}},
            {'name': 'submit_plan', 'description': 'Submit a plan for Lead approval.', 'input_schema': {'type': 'object', 'properties': {'plan': {'type': 'string'}}, 'required': ['plan']}},
            {'name': 'list_tasks', 'description': 'List all tasks.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
            {'name': 'claim_task', 'description': 'Claim a pending task.', 'input_schema': {'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']}},
            {'name': 'complete_task', 'description': 'Mark an in-progress task as completed.', 'input_schema': {'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']}},
        ]

        all_sub_handlers = {
            'bash': _run_bash,
            'read_file': _run_read,
            'write_file': _run_write,
            'send_message': lambda to, content: (BUS.send(name, to, content), 'Sent')[1],
            'list_tasks': _run_list_tasks,
            'claim_task': _run_claim_task,
            'complete_task': _run_complete_task,
        }
        sub_tools = [tool for tool in all_sub_tools if tool['name'] in role_cfg['allowed_tools']]
        sub_handlers = {tool_name: handler for tool_name, handler in all_sub_handlers.items() if tool_name in role_cfg['allowed_tools']}

        while True:
            _update_teammate_state(name, status=core_state.active_teammates.get(name, {}).get('status', 'running'), last_event='loop')
            if len(messages) <= 3:
                messages.insert(0, {'role': 'user', 'content': f"<identity>You are '{name}', role: {role_key}. Continue your work.</identity>"})
            should_shutdown = False
            for _ in range(10):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    stopped = handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if protocol_ctx['waiting_plan']:
                    time.sleep(IDLE_POLL_INTERVAL)
                    continue
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox if m.get('type') == 'message']
                    if non_protocol:
                        messages.append({'role': 'user', 'content': '<inbox>' + json.dumps(non_protocol) + '</inbox>'})
                try:
                    response = client.messages.create(model=MODEL, system=system, messages=messages[-20:], tools=sub_tools, max_tokens=8000)
                except Exception as e:
                    if current_task_id['value']:
                        requeue_task(current_task_id['value'], reason=f'teammate {name} llm error: {type(e).__name__}: {e}')
                    break
                messages.append({'role': 'assistant', 'content': response.content})
                if not has_tool_use(response.content):
                    break
                results = []
                for block in response.content:
                    if block.type == 'tool_use':
                        if block.name == 'submit_plan':
                            output = _teammate_submit_plan(name, block.input.get('plan', ''))
                            match = re.search(r'\((req_\d+)\)', output)
                            protocol_ctx['waiting_plan'] = match.group(1) if match else output
                            _update_teammate_state(name, waiting_plan=protocol_ctx['waiting_plan'], status='waiting_plan_approval', last_event='submitted_plan')
                        else:
                            handler = sub_handlers.get(block.name)
                            output = call_tool_handler(handler, block.input, block.name)
                        results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': str(output)})
                        if protocol_ctx['waiting_plan']:
                            break
                messages.append({'role': 'user', 'content': results})
                if protocol_ctx['waiting_plan']:
                    break
            if should_shutdown:
                break
            if protocol_ctx['waiting_plan']:
                continue
            idle_result = idle_poll(name, messages, name, role_key, wt_ctx)
            if idle_result in ('shutdown', 'timeout'):
                if idle_result == 'timeout' and current_task_id['value']:
                    requeue_task(current_task_id['value'], reason=f'teammate {name} timed out while owning task')
                break

        summary = 'Done.'
        for msg in reversed(messages):
            if msg['role'] == 'assistant' and isinstance(msg['content'], list):
                for b in msg['content']:
                    if getattr(b, 'type', None) == 'text':
                        summary = b.text
                        break
                else:
                    continue
                break
        BUS.send(name, 'lead', summary, 'result')
        remember(
            f'teammate-{name}-result',
            f'Teammate finished: {name}',
            f"Teammate '{name}' finished with role '{role_key}'.\nFinal summary: {summary[:800]}",
            note_type='event',
        )
        core_state.active_teammates.pop(name, None)

    core_state.active_teammates[name] = {
        'role': role_key,
        'status': 'running',
        'task_id': '',
        'task_subject': '',
        'worktree': '',
        'waiting_plan': None,
        'last_event': 'spawned',
        'started_at': time.time(),
        'updated_at': time.time(),
    }
    threading.Thread(target=run, daemon=True).start()
    return f"Teammate '{name}' spawned as {role}"
