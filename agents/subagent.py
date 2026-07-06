from __future__ import annotations

from core.client import client
from core.config import MODEL, WORKDIR
from hooks.pipeline import trigger_hooks
from tools.builtin import call_tool_handler
from tools.bash_ops import run_bash
from tools.file_ops import run_edit, run_glob, run_read, run_write

SUB_SYSTEM = (
    f'You are a coding subagent at {WORKDIR}. '
    'Complete the task, then return a concise final summary. '
    'Do not spawn more agents.'
)

SUB_TOOLS = [
    {'name': 'bash', 'description': 'Run a shell command.', 'input_schema': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']}},
    {'name': 'read_file', 'description': 'Read file contents.', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'limit': {'type': 'integer'}, 'offset': {'type': 'integer'}}, 'required': ['path']}},
    {'name': 'write_file', 'description': 'Write content to a file.', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}}, 'required': ['path', 'content']}},
    {'name': 'edit_file', 'description': 'Replace exact text in a file once.', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'old_text': {'type': 'string'}, 'new_text': {'type': 'string'}}, 'required': ['path', 'old_text', 'new_text']}},
    {'name': 'glob', 'description': 'Find files matching a glob pattern.', 'input_schema': {'type': 'object', 'properties': {'pattern': {'type': 'string'}}, 'required': ['pattern']}},
]

SUB_HANDLERS = {
    'bash': run_bash,
    'read_file': run_read,
    'write_file': run_write,
    'edit_file': run_edit,
    'glob': run_glob,
}


def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return '\n'.join(getattr(block, 'text', '') for block in content if getattr(block, 'type', None) == 'text').strip()


def has_tool_use(content) -> bool:
    return any(getattr(block, 'type', None) == 'tool_use' for block in content)


def spawn_subagent(description: str) -> str:
    messages = [{'role': 'user', 'content': description}]
    for _ in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM, messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({'role': 'assistant', 'content': response.content})
        if not has_tool_use(response.content):
            break
        results = []
        for block in response.content:
            if block.type != 'tool_use':
                continue
            blocked = trigger_hooks('PreToolUse', block)
            if blocked:
                output = str(blocked)
            else:
                handler = SUB_HANDLERS.get(block.name)
                output = call_tool_handler(handler, block.input, block.name)
                trigger_hooks('PostToolUse', block, output)
            results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': str(output)})
        messages.append({'role': 'user', 'content': results})
    for msg in reversed(messages):
        if msg['role'] == 'assistant':
            text = extract_text(msg['content'])
            if text:
                return text
    return 'Subagent finished without a text summary.'

