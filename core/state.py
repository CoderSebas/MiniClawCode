from __future__ import annotations

import threading
from typing import Any

CURRENT_TODOS: list[dict] = []
active_teammates: dict[str, dict[str, Any]] = {}
pending_requests: dict[str, Any] = {}
rounds_since_todo = 0
agent_lock = threading.Lock()
