from __future__ import annotations

import ast
import json
import re

import core.state as core_state
from bus.message_bus import BUS
from skills.loader import load_skill
from tools.bash_ops import run_bash
from tools.file_ops import run_edit, run_glob, run_read, run_write
from tools.task_tools import auto_plan_tasks, run_claim_task, run_complete_task, run_create_task, run_get_task, run_list_tasks, run_requeue_task


def call_tool_handler(handler, args: dict, name: str) -> str:
    if not handler:
        return f'Unknown: {name}'
    try:
        return handler(**(args or {}))
    except TypeError as e:
        return f'Error: {e}'


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, 'Error: todos must be a list or JSON array string'
    if not isinstance(todos, list):
        return None, 'Error: todos must be a list'
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f'Error: todos[{i}] must be an object'
        if 'content' not in todo or 'status' not in todo:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if todo['status'] not in ('pending', 'in_progress', 'completed'):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    todos, error = _normalize_todos(todos)
    if error:
        return error
    core_state.CURRENT_TODOS = todos
    print(f'  \033[33m[todo] updated {len(core_state.CURRENT_TODOS)} item(s)\033[0m')
    return f'Updated {len(core_state.CURRENT_TODOS)} todos'


def run_task_subagent(description: str) -> str:
    from agents.subagent import spawn_subagent

    return spawn_subagent(description)


def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    from agents.teammate import spawn_teammate_thread

    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    BUS.send('lead', to, content)
    return f'Sent to {to}'


def run_check_inbox() -> str:
    from agents.protocol import consume_lead_inbox

    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return '(inbox empty)'
    lines = []
    for m in msgs:
        meta = m.get('metadata', {})
        req_id = meta.get('request_id', '')
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return '\n'.join(lines)


def run_request_shutdown(teammate: str) -> str:
    from agents.protocol import run_request_shutdown as _run_request_shutdown

    return _run_request_shutdown(teammate)


def run_request_plan(teammate: str, task: str) -> str:
    from agents.protocol import run_request_plan as _run_request_plan

    return _run_request_plan(teammate, task)


def run_review_plan(request_id: str, approve: bool, feedback: str = '') -> str:
    from agents.protocol import run_review_plan as _run_review_plan

    return _run_review_plan(request_id, approve, feedback)


def run_create_worktree(name: str, task_id: str = '') -> str:
    from worktree.git_ops import create_worktree

    return create_worktree(name, task_id)


def run_remove_worktree(name: str, discard_changes: bool = False) -> str:
    from worktree.git_ops import remove_worktree

    return remove_worktree(name, discard_changes)


def run_keep_worktree(name: str) -> str:
    from worktree.git_ops import keep_worktree

    return keep_worktree(name)


def run_connect_mcp(name: str) -> str:
    from mcp.manager import connect_mcp

    return connect_mcp(name)


def run_list_mcp_servers() -> str:
    from mcp.manager import list_registered_servers

    return list_registered_servers()


def run_list_connected_mcp() -> str:
    from mcp.manager import list_connected_servers

    return list_connected_servers()


def run_list_mcp_tools() -> str:
    from mcp.manager import list_mcp_tools

    return list_mcp_tools()


def run_list_teammates() -> str:
    if not core_state.active_teammates:
        return 'No active teammates.'
    lines = []
    for name, state in core_state.active_teammates.items():
        role = state.get('role', 'unknown')
        status = state.get('status', 'unknown')
        task_subject = state.get('task_subject', '')
        last_event = state.get('last_event', '')
        line = f'  {name}: role={role} status={status}'
        if task_subject:
            line += f' task={task_subject}'
        if last_event:
            line += f' last={last_event}'
        lines.append(line)
    return '\n'.join(lines)


def run_list_memory_notes() -> str:
    from context.memory_store import list_memory_note_names

    return list_memory_note_names()


def run_dedupe_memory() -> str:
    from context.memory_store import dedupe_memory_notes

    return dedupe_memory_notes()


def run_prune_memory(max_notes: int = 200) -> str:
    from context.memory_store import prune_memory_notes

    return prune_memory_notes(max_notes)


def run_auto_plan_tasks(objective: str, auto_spawn: bool = True, include_review: bool = True, include_test: bool = True) -> str:
    from agents.teammate import spawn_teammate_thread

    created = auto_plan_tasks(objective, include_review=include_review, include_test=include_test)
    spawned = []
    if auto_spawn:
        role_pairs = [('planner', created[0]), ('coder', created[1])]
        if include_review and len(created) >= 3:
            role_pairs.append(('reviewer', created[2]))
        if include_test:
            test_index = 3 if include_review else 2
            if len(created) > test_index:
                role_pairs.append(('tester', created[test_index]))
        for role, task in role_pairs:
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '-', f'{role}-{task.id[-4:]}').lower()
            spawn_teammate_thread(
                safe_name,
                role,
                (
                    f"Claim task {task.id} and complete it according to your role.\n"
                    f"Task: {task.subject}\nDescription: {task.description}\n"
                    'If the task is blocked, wait and retry later instead of fabricating progress.'
                ),
            )
            spawned.append(f'{safe_name}:{role}')
    summary = [f'Created {len(created)} tasks for objective: {objective}']
    summary.extend(f'  {task.id}: {task.subject}' for task in created)
    if spawned:
        summary.append('Spawned teammates: ' + ', '.join(spawned))
    return '\n'.join(summary)


BUILTIN_TOOLS = [
    {'name': 'bash', 'description': 'Run a shell command.', 'input_schema': {'type': 'object', 'properties': {'command': {'type': 'string'}, 'run_in_background': {'type': 'boolean'}}, 'required': ['command']}},
    {'name': 'read_file', 'description': 'Read file contents.', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'limit': {'type': 'integer'}, 'offset': {'type': 'integer'}}, 'required': ['path']}},
    {'name': 'write_file', 'description': 'Write content to a file.', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}}, 'required': ['path', 'content']}},
    {'name': 'edit_file', 'description': 'Replace exact text in a file once.', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'old_text': {'type': 'string'}, 'new_text': {'type': 'string'}}, 'required': ['path', 'old_text', 'new_text']}},
    {'name': 'glob', 'description': 'Find files matching a glob pattern.', 'input_schema': {'type': 'object', 'properties': {'pattern': {'type': 'string'}}, 'required': ['pattern']}},
    {'name': 'todo_write', 'description': 'Create and manage a task list for the current session.', 'input_schema': {'type': 'object', 'properties': {'todos': {'type': 'array', 'items': {'type': 'object', 'properties': {'content': {'type': 'string'}, 'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed']}}, 'required': ['content', 'status']}}}, 'required': ['todos']}},
    {'name': 'task', 'description': 'Launch a focused subagent. Returns only its final summary.', 'input_schema': {'type': 'object', 'properties': {'description': {'type': 'string'}}, 'required': ['description']}},
    {'name': 'load_skill', 'description': 'Load the full content of a skill by name.', 'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}},
    {'name': 'compact', 'description': 'Summarize earlier conversation and continue with compacted context.', 'input_schema': {'type': 'object', 'properties': {'focus': {'type': 'string'}}, 'required': []}},
    {'name': 'create_task', 'description': 'Create a task.', 'input_schema': {'type': 'object', 'properties': {'subject': {'type': 'string'}, 'description': {'type': 'string'}, 'blockedBy': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['subject']}},
    {'name': 'list_tasks', 'description': 'List all tasks.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'get_task', 'description': 'Get full task details.', 'input_schema': {'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']}},
    {'name': 'claim_task', 'description': 'Claim a pending task.', 'input_schema': {'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']}},
    {'name': 'complete_task', 'description': 'Complete an in-progress task.', 'input_schema': {'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']}},
    {'name': 'requeue_task', 'description': 'Requeue a stuck or failed task back to pending.', 'input_schema': {'type': 'object', 'properties': {'task_id': {'type': 'string'}, 'reason': {'type': 'string'}}, 'required': ['task_id']}},
    {'name': 'schedule_cron', 'description': 'Schedule a cron job. cron is 5-field: min hour dom month dow. For one-shot reminders, compute the target minute and set recurring=false.', 'input_schema': {'type': 'object', 'properties': {'cron': {'type': 'string'}, 'prompt': {'type': 'string'}, 'recurring': {'type': 'boolean'}, 'durable': {'type': 'boolean'}}, 'required': ['cron', 'prompt']}},
    {'name': 'list_crons', 'description': 'List registered cron jobs.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'cancel_cron', 'description': 'Cancel a cron job by ID.', 'input_schema': {'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id']}},
    {'name': 'spawn_teammate', 'description': 'Spawn an autonomous teammate.', 'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'role': {'type': 'string'}, 'prompt': {'type': 'string'}}, 'required': ['name', 'role', 'prompt']}},
    {'name': 'send_message', 'description': 'Send message to a teammate.', 'input_schema': {'type': 'object', 'properties': {'to': {'type': 'string'}, 'content': {'type': 'string'}}, 'required': ['to', 'content']}},
    {'name': 'check_inbox', 'description': 'Check inbox for messages and protocol responses.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'request_shutdown', 'description': 'Request a teammate to shut down.', 'input_schema': {'type': 'object', 'properties': {'teammate': {'type': 'string'}}, 'required': ['teammate']}},
    {'name': 'request_plan', 'description': 'Ask a teammate to submit a plan.', 'input_schema': {'type': 'object', 'properties': {'teammate': {'type': 'string'}, 'task': {'type': 'string'}}, 'required': ['teammate', 'task']}},
    {'name': 'review_plan', 'description': 'Approve or reject a submitted plan.', 'input_schema': {'type': 'object', 'properties': {'request_id': {'type': 'string'}, 'approve': {'type': 'boolean'}, 'feedback': {'type': 'string'}}, 'required': ['request_id', 'approve']}},
    {'name': 'create_worktree', 'description': 'Create an isolated git worktree.', 'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'task_id': {'type': 'string'}}, 'required': ['name']}},
    {'name': 'remove_worktree', 'description': 'Remove a worktree. Refuses if changes exist.', 'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'discard_changes': {'type': 'boolean'}}, 'required': ['name']}},
    {'name': 'keep_worktree', 'description': 'Keep a worktree for manual review.', 'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}},
    {'name': 'connect_mcp', 'description': 'Connect to an MCP server (docs, deploy) and discover tools.', 'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}},
    {'name': 'list_mcp_servers', 'description': 'List registered MCP servers and modes.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'list_connected_mcp', 'description': 'List connected MCP servers and recent activity.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'list_mcp_tools', 'description': 'List discovered tools from connected MCP servers.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'list_teammates', 'description': 'List active teammates and their current status.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'list_memory_notes', 'description': 'List saved long-term memory notes.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'dedupe_memory', 'description': 'Remove duplicate long-term memory notes.', 'input_schema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'prune_memory', 'description': 'Keep only the newest long-term memory notes.', 'input_schema': {'type': 'object', 'properties': {'max_notes': {'type': 'integer'}}, 'required': []}},
    {'name': 'auto_plan_tasks', 'description': 'Automatically decompose an objective into tasks and optionally spawn role-based teammates.', 'input_schema': {'type': 'object', 'properties': {'objective': {'type': 'string'}, 'auto_spawn': {'type': 'boolean'}, 'include_review': {'type': 'boolean'}, 'include_test': {'type': 'boolean'}}, 'required': ['objective']}},
]

BUILTIN_HANDLERS = {
    'bash': run_bash,
    'read_file': run_read,
    'write_file': run_write,
    'edit_file': run_edit,
    'glob': run_glob,
    'todo_write': run_todo_write,
    'task': run_task_subagent,
    'load_skill': load_skill,
    'create_task': run_create_task,
    'list_tasks': run_list_tasks,
    'get_task': run_get_task,
    'claim_task': run_claim_task,
    'complete_task': run_complete_task,
    'requeue_task': run_requeue_task,
    'schedule_cron': lambda cron, prompt, recurring=True, durable=True: __import__('cron.scheduler', fromlist=['run_schedule_cron']).run_schedule_cron(cron, prompt, recurring, durable),
    'list_crons': lambda: __import__('cron.scheduler', fromlist=['run_list_crons']).run_list_crons(),
    'cancel_cron': lambda job_id: __import__('cron.scheduler', fromlist=['run_cancel_cron']).run_cancel_cron(job_id),
    'spawn_teammate': run_spawn_teammate,
    'send_message': run_send_message,
    'check_inbox': run_check_inbox,
    'request_shutdown': run_request_shutdown,
    'request_plan': run_request_plan,
    'review_plan': run_review_plan,
    'create_worktree': run_create_worktree,
    'remove_worktree': run_remove_worktree,
    'keep_worktree': run_keep_worktree,
    'connect_mcp': run_connect_mcp,
    'list_mcp_servers': run_list_mcp_servers,
    'list_connected_mcp': run_list_connected_mcp,
    'list_mcp_tools': run_list_mcp_tools,
    'list_teammates': run_list_teammates,
    'list_memory_notes': run_list_memory_notes,
    'dedupe_memory': run_dedupe_memory,
    'prune_memory': run_prune_memory,
    'auto_plan_tasks': run_auto_plan_tasks,
}
