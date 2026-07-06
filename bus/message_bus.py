from __future__ import annotations

import json
import time

from core.config import MAILBOX_DIR, print_event


class MessageBus:
    def send(self, from_agent: str, to_agent: str, content: str, msg_type: str = 'message', metadata: dict = None):
        msg = {
            'from': from_agent,
            'to': to_agent,
            'content': content,
            'type': msg_type,
            'ts': time.time(),
            'metadata': metadata or {},
        }
        inbox = MAILBOX_DIR / f'{to_agent}.jsonl'
        with open(inbox, 'a', encoding='utf-8') as f:
            f.write(json.dumps(msg) + '\n')
        print_event('mail', f'{from_agent} -> {to_agent} ({msg_type}) {content[:60]}', 179)

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f'{agent}.jsonl'
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text(encoding='utf-8').splitlines() if line.strip()]
        inbox.unlink()
        return msgs


BUS = MessageBus()
