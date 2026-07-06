from __future__ import annotations

import threading
import time

import core.config as core_config
import core.state as core_state
from agents.protocol import consume_lead_inbox
from agents.subagent import has_tool_use
from context.compaction import block_type, build_user_content, inject_background_notifications, prepare_context, reactive_compact
from context.prompt_builder import assemble_system_prompt, update_context
from core.client import RecoveryState, client, is_prompt_too_long_error, with_retry
from core.config import (
    CONTINUATION_PROMPT,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    MAX_RECOVERY_RETRIES,
    MODEL,
    PROMPT,
    print_error,
    print_event,
    print_turn_summary,
    print_welcome_banner,
    terminal_print,
)
from cron.scheduler import consume_cron_queue
from hooks.pipeline import trigger_hooks
from mcp.manager import assemble_tool_pool
from tools.bash_ops import should_run_background, start_background_task
from tools.builtin import call_tool_handler


def call_llm(messages: list, context: dict, tools: list, state: RecoveryState, max_tokens: int, assemble_system_prompt_fn):
    system = assemble_system_prompt_fn(context)
    return with_retry(
        lambda: client.messages.create(
            model=state.current_model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
        ),
        state,
    )


def agent_loop(
    messages: list,
    context: dict,
    *,
    assemble_tool_pool_fn,
    prepare_context_fn,
    update_context_fn,
    call_llm_fn,
    reactive_compact_fn,
    compact_history_fn,
    build_user_content_fn,
    inject_background_notifications_fn,
):
    tools, handlers = assemble_tool_pool_fn()
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        from agents.teammate import reap_stale_teammates

        stale_notes = reap_stale_teammates()
        for note in stale_notes:
            messages.append({'role': 'user', 'content': f'[Supervisor] {note}'})
            print_event('supervise', note, 179)
        fired = consume_cron_queue()
        for job in fired:
            messages.append({'role': 'user', 'content': f'[Scheduled] {job.prompt}'})
            print_event('cron', f'inject {job.prompt[:60]}', 141)

        inject_background_notifications_fn(messages)

        if core_state.rounds_since_todo >= 3:
            messages.append({'role': 'user', 'content': '<reminder>Update your todos.</reminder>'})
            core_state.rounds_since_todo = 0

        prepare_context_fn(messages)
        context = update_context_fn(context, messages)
        tools, handlers = assemble_tool_pool_fn()

        try:
            response = call_llm_fn(messages, context, tools, state, max_tokens, assemble_system_prompt)
        except Exception as e:
            if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                messages[:] = reactive_compact_fn(messages)
                state.has_attempted_reactive_compact = True
                continue
            messages.append({'role': 'assistant', 'content': [{'type': 'text', 'text': f'[Error] {type(e).__name__}: {e}'}]})
            print_error(f'{type(e).__name__}: {e}')
            return

        if response.stop_reason == 'max_tokens':
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print_event('retry', f'max_tokens -> retry with {max_tokens}', 179)
                continue
            messages.append({'role': 'assistant', 'content': response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({'role': 'user', 'content': CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            return

        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append({'role': 'assistant', 'content': response.content})
        if not has_tool_use(response.content):
            trigger_hooks('Stop', messages)
            return

        results = []
        compacted_now = False
        for block in response.content:
            if block.type != 'tool_use':
                continue
            print_event('call', block.name, 81)

            if block.name == 'compact':
                messages[:] = compact_history_fn(messages)
                messages.append({'role': 'user', 'content': '[Compacted. Continue with summarized context.]'})
                compacted_now = True
                break

            blocked = trigger_hooks('PreToolUse', block)
            if blocked:
                results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': str(blocked)})
                continue

            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block, handlers)
                output = f'[Background task {bg_id} started] Result will arrive as a task_notification.'
                results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': output})
                continue

            handler = handlers.get(block.name)
            output = call_tool_handler(handler, block.input, block.name)
            trigger_hooks('PostToolUse', block, output)
            print_event('result', str(output)[:300], 244)

            if block.name == 'todo_write':
                core_state.rounds_since_todo = 0
            else:
                core_state.rounds_since_todo += 1

            results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': output})

        if compacted_now:
            continue

        messages.append({'role': 'user', 'content': build_user_content_fn(results)})


def print_turn_assistants(messages: list, turn_start: int):
    for msg in messages[turn_start:]:
        if msg.get('role') != 'assistant':
            continue
        for block in msg.get('content', []):
            if block_type(block) == 'text':
                terminal_print(block['text'] if isinstance(block, dict) else block.text)


def cron_autorun_loop(history: list, context: dict):
    from context.compaction import compact_history

    while True:
        time.sleep(1)
        fired = consume_cron_queue()
        if not fired:
            continue
        with core_state.agent_lock:
            turn_start = len(history)
            for job in fired:
                history.append({'role': 'user', 'content': f'[Scheduled] {job.prompt}'})
                print_event('cron', f'auto {job.prompt[:60]}', 141)
            agent_loop(
                history,
                context,
                assemble_tool_pool_fn=assemble_tool_pool,
                prepare_context_fn=prepare_context,
                update_context_fn=update_context,
                call_llm_fn=call_llm,
                reactive_compact_fn=reactive_compact,
                compact_history_fn=compact_history,
                build_user_content_fn=build_user_content,
                inject_background_notifications_fn=inject_background_notifications,
            )
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)


def main():
    from context.compaction import compact_history

    core_config.CLI_ACTIVE = True
    print_welcome_banner()
    print()
    history = []
    context = update_context({}, [])
    threading.Thread(target=cron_autorun_loop, args=(history, context), daemon=True).start()
    while True:
        try:
            query = input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ('q', 'exit', ''):
            break
        trigger_hooks('UserPromptSubmit', query)
        turn_start = len(history)
        history.append({'role': 'user', 'content': query})
        with core_state.agent_lock:
            agent_loop(
                history,
                context,
                assemble_tool_pool_fn=assemble_tool_pool,
                prepare_context_fn=prepare_context,
                update_context_fn=update_context,
                call_llm_fn=call_llm,
                reactive_compact_fn=reactive_compact,
                compact_history_fn=compact_history,
                build_user_content_fn=build_user_content,
                inject_background_notifications_fn=inject_background_notifications,
            )
            context = update_context(context, history)
            print_turn_assistants(history, turn_start)
            tool_count = 0
            for msg in history[turn_start:]:
                content = msg.get('content')
                if isinstance(content, list):
                    tool_count += sum(
                        1 for item in content
                        if isinstance(item, dict) and item.get('type') == 'tool_result'
                    )
            print_turn_summary(
                tool_count=tool_count,
                teammate_count=len(core_state.active_teammates),
                mcp_count=len(context.get('connected_mcp', [])),
                todo_count=len(core_state.CURRENT_TODOS),
            )

        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:
            def inbox_label(msg):
                req_id = msg.get('metadata', {}).get('request_id', '')
                suffix = f' req:{req_id}' if req_id else ''
                return f"{msg.get('type', 'message')}{suffix}"

            inbox_text = '\n'.join(f"From {m['from']} [{inbox_label(m)}]: {m['content'][:200]}" for m in inbox)
            history.append({'role': 'user', 'content': f'[Inbox]\n{inbox_text}'})
            print_event('inbox', f'{len(inbox)} message(s) received', 180)
        print()


if __name__ == '__main__':
    main()
