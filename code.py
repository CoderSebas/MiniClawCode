import os,subprocess,json,time
from pathlib import Path

# for macOS
try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TASKS_DIR = WORKDIR / ".tasks"; TASKS_DIR.mkdir(exist_ok=True)
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# Skill catalog scan (used by build_system below)
def _parse_frontmatter(text:str) -> tuple[dict,str]:
    """Parse YAML frontmatter from SKILL.md. Returns (meta, body)."""
    if not text.startswith("---"):
        return {},text
    parts = text.split("---",2)
    if len(parts) < 3:
        return {},text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k,v = line.split(":",1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta,parts[2].strip()

# Build skill registry at startup (used for safe lookup in load_skill)
SKILL_REGISTRY:dict[str,dict] = {}

def _scan_skills():
    """Scan skills/ dir, populate SKILL_REGISTRY with name/description/content."""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta,body = _parse_frontmatter(raw)
            name = meta.get("name",d.name)
            desc = meta.get("description",raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name":name,"description":desc,"content":raw}

_scan_skills()

def list_skills() -> str:
    """List all skills (name + one-line description)."""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

# SYSTEM includes skill catalog (cheap — just names + descriptions)
def build_system() -> str:
    """Build SYSTEM prompt with skill catalog injected at startup."""
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}."
        "Before starting any multi-step task, use todo_write to plan your steps. "
        "Update status as you go."
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )

# # SYSTEM prompt adds planning guidance
# SYSTEM = (
#     f"You are a coding agent at {WORKDIR}. "
#     "Before starting any multi-step task, use todo_write to plan your steps. "
#     "Update status as you go."
# )
SYSTEM = build_system()

# subagent gets its own system prompt — no task, no recursion, no compact, no skill loading
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)

# ── Tool execution ────────────────────────────────────────
def run_bash(command:str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command,shell=True,cwd=os.getcwd(),
                           capture_output=True,text=True,timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"
    
def safe_path(p:str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_read(path:str, limit:int|None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit<len(lines):
            lines = lines[:limit] + [f"...({len(lines) - limit}) more lines"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error:{e}"
    
def run_write(path:str, content:str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error:{e}"
    
def run_edit(path:str, old_text:str, new_text:str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text,new_text,1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error:{e}"

def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if(WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error:{e}"

def run_todo_write(todos:list) -> str:
    # validate required fields
    for i,t in enumerate(todos):
        if "content" not in t or "status" not in t:
            return f"Errors: todos[{i}] missing 'content' or 'status'."
        if t["status"] not in ("pending", "in_progress", "completed"):
            return f"Errors: todos[{i}] has invalid status '{t['status']}'"
    tasks_file = TASKS_DIR / "current_todos.json"
    tasks_file.write_text(json.dumps(todos,indent=2,ensure_ascii=False)) # Output the todos to a json file
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in todos:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines)) # Display current status in terminal
    return f"Updated {len(todos)} tasks"
    
def extract_text(content) -> str:
    """Extract text from message content blocks."""
    if not isinstance(content,list):
        return str(content)
    return "\n".join(getattr(b,"text","") for b in content if getattr(b,"type",None) == "text")

# Subagent — fresh messages[], summary only

SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]
# NO "task" tool — prevent recursive spawning

SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}

def spawn_subagent(description:str) -> str:
    """Spawn a subagent with fresh messages[], return summary only."""
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]  # fresh context

    for _ in range(30): # safety limit
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000,
        )

        messages.append({"role":"assistant","content":response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # Issue 1: subagent also runs hooks (permissions apply)
                blocked = trigger_hooks("PreToolUse",block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": output})
        messages.append({"role": "user", "content": results})

    # Issue 5: fallback if safety limit hit during tool_use
    result = extract_text(messages[-1]["content"])
    if not result:
        # last message is tool_result, look backwards for assistant text
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result  # only summary, entire message history discarded

# load_skill — runtime full content loading
def load_skill(name:str) -> str:
    """Load full skill content. Lookup via registry — no path traversal."""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"skill not found:{name}"
    return skill["content"]

# ═══════════════════════════════════════════════════════════
# Four-Layer Compaction Pipeline
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000
KEEP_RECENT = 3
PERSIST_THRESHOLD = 30000

def estimate_size(msgs): return len(str(msgs))

# L1: snipCompact — trim middle messages
def snip_compact(messages, max_messages=50):
    if len(messages) < max_messages: return messages
    keep_head, keep_tail = 3, max_messages - 3
    snipped = len(messages) - keep_head - keep_tail
    return messages[:keep_head] + [{"role":"user","content":f"[snipped] {snipped} messages"}] + messages[-keep_tail:]

# L2: microCompact — old result placeholders
def collect_tool_results(messages):
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg["content"],list): continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block,dict) and block.get("type") == "tool_result":
                blocks.append((mi,bi,block))
    return blocks

def micro_compact(messages):
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT: return messages
    for _, _, block in tool_results[:-KEEP_RECENT]:
        if len(block.get("content","")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages

# L3: toolResultBudget — persist large results to disk
def persist_large_output(tool_use_id, output):
    if len(output) <= PERSIST_THRESHOLD: return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists(): path.write_text(output)
    return f"<persisted-output>\nFull output:{path}\nPreview:\n{output[:2000]}\n<persisted-output>"

def tool_result_budget(messages, max_bytes=200_000):
    last = messages[-1] if messages else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return messages
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes: return messages
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for _, block in ranked:
        if total <= max_bytes: break
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD: continue
        tid = block.get("tool_use_id", "unknown")
        block["content"] = persist_large_output(tid, content)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages

# L4: autoCompact — LLM full summary
def write_transcript(messages):
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages: f.write(json.dumps(msg,default=str) + "\n")
    return path

def summarize_history(messages):
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    response = client.messages.create(model=MODEL, messages=[{"role":"user","content":prompt}],max_tokens=2000)
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text").strip() or "(empty summary)"

def compact_history(messages):
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role":"user","content":f"[Compacted]\n\n{summary}"}]

# Emergency: reactiveCompact — on API error
def reactive_compact(messages):
    transcript = write_transcript(messages)
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[-5:]]

# ── Tool definition: just bash ────────────────────────────
TOOLS = [
    {"name":"bash","description":"Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name":"task","description":"Launch a subagent to handle a complex subtask. Returns only the final conclusion.",# Add task tool to parent's tools
    "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    # s07: skill tool (catalog is already in SYSTEM prompt, this loads full content)
    {"name":"load_skill","description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
     # s08 change: new compact tool — triggers compact_history, not a no-op
    {"name": "compact", "description": "Summarize earlier conversation to free context space.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task":spawn_subagent,"load_skill":load_skill
}

# ═══════════════════════════════════════════════════════════
# Hook System
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit":[],"PreToolUse":[],"PostToolUse":[],"Stop":[]}

def register_hook(event:str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event:str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:  # teaching shortcut: block this tool call
            return result
    return None

# permission check logic, now wrapped as a hook
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

def permission_hook(block):
    """PreToolUse: s03 check_permission() logic moved here."""
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None

def log_hook(block):
    """PreToolUse: log every tool call."""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None

def large_output_hook(block,output):
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}:{len(str(output))} chars\033[0m")
    return None

# UserPromptSubmit hook: log user input before it reaches the LLM
def context_inject_hook(query:str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

# Stop hook: print summary when loop is about to exit
def summary_hook(messages:list):
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("PreToolUse",permission_hook)
register_hook("PreToolUse",log_hook)
register_hook("PostToolUse",large_output_hook)
register_hook("UserPromptSubmit",context_inject_hook)
register_hook("Stop",summary_hook)

# ═══════════════════════════════════════════════════════════
#  agent_loop — core: nag reminder, task auto-dispatches, run compaction pipeline before LLM
# ═══════════════════════════════════════════════════════════

round_since_todo = 0
MAX_REACTIVE_RETRIES = 1  # retry limit for reactive compact

def agent_loop(messages:list):
    global round_since_todo
    reactive_retries = 0
    while True:
        # three preprocessors (0 API calls, cheap first)
        # Order matches CC source: budget → snip → micro
        messages[:] = tool_result_budget(messages)      # L3: persist large results first
        messages[:] = snip_compact(messages)            # L1: trim middle
        messages[:] = micro_compact(messages)           # L2: old result placeholders

        # tokens still over threshold → LLM summary (1 API call)
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[Auto compact]")
            messages[:] = compact_history(messages)


        # nag reminder — inject if model hasn't updated todos for 3 rounds
        if round_since_todo >=3 and messages:
            messages.append({"role":"user","content":"<reminder>Update your todos.</reminder>"})
            round_since_todo = 0

        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM, messages=messages,
                tools=TOOLS, max_tokens=8000,
            )
            reactive_retries = 0    # reset on successful API call
        except Exception as e:
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise
        
        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        # If the model didn't call a tool, we're done
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop",messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return
        
        # Execute each tool call, collect results
        round_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # compact tool triggers compact_history, not a no-op string
            if block.name == "compact":
                messages[:] = compact_history(messages)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "[Compacted. Conversation history has been summarized.]"})
                messages.append({"role": "user", "content": results})
                break  # end current turn, start fresh with compacted context

            # hook replaces hard-coded check_permission()
            blocked = trigger_hooks("PreToolUse",block) # pre hook
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown:{block.name}"
            trigger_hooks("PostToolUse",block,output) # post hook
            print(str(output)[:200])

            # reset nag counter when todo_write is called
            if block.name == "todo_write":
                round_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        else:
            # normal path: no compact was called
            # Feed tool results back, loop continues
            messages.append({"role": "user", "content": results})
            continue
        # compact was called: results already appended above
        continue



# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print("Welcome to MiniClawCode!")
    print("Skill Loading — catalog in SYSTEM, content on demand")
    print("Type a question, press Enter. Type q to quit.\n")
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except(EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q","exit",""):
            break
        trigger_hooks("UserPromptSubmit",query)
        history.append({"role":"user","content":query})
        agent_loop(history)
        # Print the model's final text response
        response_content = history[-1]["content"]
        if isinstance(response_content,list):
            for block in response_content:
                if getattr(block,"type",None) == "text":
                    print(block.text)
        print()

