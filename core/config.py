from __future__ import annotations

import os
import threading
from pathlib import Path

from dotenv import load_dotenv

try:
    import readline

    readline.parse_and_bind('set bind-tty-special-chars off')
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

load_dotenv(override=True)
if os.getenv('ANTHROPIC_BASE_URL'):
    os.environ.pop('ANTHROPIC_AUTH_TOKEN', None)

WORKDIR = Path.cwd()
ANTHROPIC_BASE_URL = os.getenv('ANTHROPIC_BASE_URL', '')
MODEL = os.environ['MODEL_ID']
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv('FALLBACK_MODEL_ID')
MODEL_PROVIDER = os.getenv('MODEL_PROVIDER', '')
MODEL_LABEL = os.getenv('MODEL_LABEL', '')

SKILLS_DIR = WORKDIR / 'skills'
TRANSCRIPT_DIR = WORKDIR / '.transcripts'
TOOL_RESULTS_DIR = WORKDIR / '.task_outputs' / 'tool-results'
TASKS_DIR = WORKDIR / '.tasks'
WORKTREES_DIR = WORKDIR / '.worktrees'
MAILBOX_DIR = WORKDIR / '.mailboxes'
MEMORY_DIR = WORKDIR / '.memory'
MEMORY_INDEX = MEMORY_DIR / 'MEMORY.md'
DURABLE_PATH = WORKDIR / '.scheduled_tasks.json'
LOGS_DIR = WORKDIR / '.logs'
ACTION_LOG_PATH = LOGS_DIR / 'agent-actions.jsonl'
MCP_CONFIG_PATH = WORKDIR / '.mcp_servers.json'

TASKS_DIR.mkdir(exist_ok=True)
WORKTREES_DIR.mkdir(exist_ok=True)
MAILBOX_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
MAX_MEMORY_NOTES = 200
TEAMMATE_STALE_SECONDS = 300
CONTINUATION_PROMPT = 'Continue from the previous response. Do not repeat completed work.'
PROMPT = '\033[38;5;117mMiniClaw > \033[0m'
CLI_ACTIVE = False


def infer_model_provider() -> str:
    if MODEL_PROVIDER:
        return MODEL_PROVIDER
    model_lower = MODEL.lower()
    base_url_lower = ANTHROPIC_BASE_URL.lower()
    if 'deepseek' in model_lower or 'deepseek' in base_url_lower:
        return 'DeepSeek'
    if 'openai' in model_lower or 'openai' in base_url_lower:
        return 'OpenAI-compatible'
    if ANTHROPIC_BASE_URL:
        return 'Custom Anthropic-compatible'
    return 'Anthropic'


def get_runtime_identity() -> dict[str, str]:
    provider = infer_model_provider()
    label = MODEL_LABEL or MODEL
    base_url = ANTHROPIC_BASE_URL or '(default Anthropic endpoint)'
    return {
        'provider': provider,
        'model': MODEL,
        'label': label,
        'base_url': base_url,
    }


def _ui(text: str, color: int, bold: bool = False) -> str:
    weight = '1;' if bold else ''
    return f'\033[{weight}38;5;{color}m{text}\033[0m'


def format_event(tag: str, text: str, color: int = 110) -> str:
    return f"{_ui(tag.upper().ljust(7), color, bold=True)} {text}"


def print_event(tag: str, text: str, color: int = 110):
    terminal_print(format_event(tag, text, color))


def print_error(text: str):
    terminal_print(format_event('error', text, 203))


def print_turn_summary(tool_count: int, teammate_count: int, mcp_count: int, todo_count: int):
    terminal_print(
        format_event(
            'status',
            f'tools={tool_count} teammates={teammate_count} mcp={mcp_count} todos={todo_count}',
            67,
        )
    )


def _panel_line(left: str = '', right: str = '', width: int = 78) -> str:
    content_width = max(width - 4, 20)
    right = right.strip()
    if right:
        available = max(content_width - len(right) - 1, 0)
        left = left[:available]
        padding = ' ' * max(content_width - len(left) - len(right), 0)
        body = f'{left}{padding}{right}'
    else:
        body = left[:content_width].ljust(content_width)
    return _ui(f'| {body} |', 110)


def print_welcome_banner():
    icon = '    /\\'
    icon_2 = ' __/  \\__'
    icon_3 = ' \\  ||  /'
    icon_4 = '  \\_||_/'
    icon_5 = '    ||'
    lines = [
        _ui('+' + '-' * 78 + '+', 110),
        _panel_line('MiniClawCode Command Console', icon),
        _panel_line('Mine your codebase. Shape your tools.', icon_2),
        _panel_line(f'CWD: {WORKDIR.name}', icon_3),
        _panel_line('', icon_4),
        _panel_line('', icon_5),
        _panel_line('Type your request and press Enter to send.', 'q to quit'),
        _panel_line('Tools, inbox, cron, and teammate events will appear inline.'),
        _ui('+' + '-' * 78 + '+', 110),
    ]
    for line in lines:
        print(line)


def terminal_print(text: str):
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        print(text)
        return
    line = ''
    if READLINE_AVAILABLE:
        try:
            line = readline.get_line_buffer()
        except Exception:
            line = ''
    print(f'\r\033[K{text}')
    print(PROMPT + line, end='', flush=True)
