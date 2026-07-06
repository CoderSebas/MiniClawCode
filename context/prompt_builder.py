from __future__ import annotations

from datetime import datetime

from core.config import MEMORY_INDEX, WORKDIR, get_runtime_identity
from context.memory_store import select_relevant_memories
from mcp.manager import mcp_clients
from skills.loader import list_skills
import core.state as core_state

PROMPT_SECTIONS = {
    'identity': "You are a coding agent. Act, don't explain.",
    'tools': 'Available tools: bash, read_file, write_file, edit_file, glob, todo_write, task, load_skill, compact, create_task, list_tasks, get_task, claim_task, complete_task, requeue_task, auto_plan_tasks, schedule_cron, list_crons, cancel_cron, spawn_teammate, list_teammates, send_message, check_inbox, request_shutdown, request_plan, review_plan, create_worktree, remove_worktree, keep_worktree, connect_mcp, list_mcp_servers, list_connected_mcp, list_mcp_tools, list_memory_notes, dedupe_memory, prune_memory. MCP tools are prefixed mcp__{server}__{tool}.',
    'workspace': f'Working directory: {WORKDIR}',
    'memory': 'Relevant memories are injected below when available.',
}


def assemble_system_prompt(context: dict) -> str:
    runtime_identity = get_runtime_identity()
    sections = [PROMPT_SECTIONS['identity'], PROMPT_SECTIONS['tools'], PROMPT_SECTIONS['workspace']]
    sections.append(
        'Runtime identity:\n'
        f"- Provider: {runtime_identity['provider']}\n"
        f"- Model ID: {runtime_identity['model']}\n"
        f"- Model label: {runtime_identity['label']}\n"
        f"- Base URL: {runtime_identity['base_url']}\n"
        'When the user asks who you are, which model you are using, or which provider you are connected to, '
        'answer strictly from this runtime identity. Do not claim to be Claude unless the runtime identity says Anthropic/Claude.'
    )
    sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
    sections.append('Skills catalog:\n' + list_skills() + '\nUse load_skill(name) when a skill is relevant.')
    if context.get('memories'):
        sections.append(f"Relevant memories:\n{context['memories']}")
    mcp_names = list(mcp_clients.keys())
    if mcp_names:
        sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")
    return '\n\n'.join(sections)


def update_context(context: dict, messages: list) -> dict:
    latest_user_query = ''
    for msg in reversed(messages):
        if msg.get('role') == 'user' and isinstance(msg.get('content'), str):
            latest_user_query = msg['content']
            break
    memories = select_relevant_memories(latest_user_query, limit_chars=2000)
    return {
        'memories': memories,
        'connected_mcp': list(mcp_clients.keys()),
        'active_teammates': list(core_state.active_teammates.keys()),
        'teammate_states': dict(core_state.active_teammates),
    }
