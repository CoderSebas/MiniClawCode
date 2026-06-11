import os,subprocess,json,time,re
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
MEMORY_DIR = WORKDIR / ".memory"; MEMORY_DIR.mkdir(parents=True,exist_ok=True)
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TASKS_DIR = WORKDIR / ".tasks"; TASKS_DIR.mkdir(exist_ok=True)
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# ── Prompt Sections ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: {tools}.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}

def assemble_system_prompt(context: dict) -> str:
    """Select and join prompt sections based on current context."""
    sections = []

    # Always loaded — identity, tools, workspace
    sections.append(PROMPT_SECTIONS["identity"])
    sections.append(PROMPT_SECTIONS["workspace"])
    tools = context.get("enabled_tools") or ["bash", "read_file", "write_file"]
    sections.append(
        PROMPT_SECTIONS["tools"].format(tools=",".join(tools))
    )

    sections.append(
        "Before starting any multi-step task, use todo_write to plan your steps. "
        "Update status as you go."
    )

    skills_catalog = context.get("skills_catalog", "")
    if skills_catalog:
        sections.append(
            "Skills available:\n"
            f"{skills_catalog}\n\n"
            "Use load_skill to get full details when needed."
        )

    # Conditional — memory loaded when MEMORY.md exists and has content
    memories = context.get("memories","")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")

    return "\n\n".join(sections)

# ── Context ──
def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state: which tools exist, whether memory files exist."""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "skills_catalog": list_skills(),
        "memories": memories,
    }

_last_context_key = None
_last_prompt = None

def get_system_prompt(context: dict) -> str:
    """Cache wrapper — reassemble only when context changes.

    Uses json.dumps for deterministic serialization, not Python's hash()
    which has process randomization and fails on nested dicts/lists.
    This cache only avoids redundant string assembly within a process.
    Real Claude Code additionally protects API-level prompt cache via
    stable section ordering and SYSTEM_PROMPT_DYNAMIC_BOUNDARY.
    """
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=True, default=str)
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memories")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt

# ═══════════════════════════════════════════════════════════
# Memory System
# ═══════════════════════════════════════════════════════════
MEMORY_TYPES = ["user", "feedback", "project", "reference"]

def _parse_frontmatter(text:str) -> tuple[dict,str]:
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

def write_memory_file(name:str, mem_type:str, description:str, body:str):
    """Write a single memory file with YAML frontmatter."""
    slug = name.lower().replace(" ","-").replace("/","-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname:{name}\ndescription:{description}\ntype:{mem_type}\n---\n\n{body}\n"
    )
    _rebuild_index()
    return filepath

def _rebuild_index():
    """Rebuild MEMORY.md index from all memory files."""
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta,body = _parse_frontmatter(raw)
        name = meta.get("name",f.stem)
        desc = meta.get("description",body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) - {desc}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")

def read_memory_index() -> str:
    """Read MEMORY.md index (injected into SYSTEM every turn)."""
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text().strip()
    return text if text else ""

def read_memory_file(filename:str) -> str|None:
    """Read a single memory file's full content."""
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text()

def list_memory_files() -> list[dict]:
    """List all memory files with metadata."""
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta,body = _parse_frontmatter(raw)
        result.append({
            "filename": f.name,
            "name": meta.get("name",f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type","user"),
            "body": body
        })
    return result

def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """Select relevant memory filenames by matching recent conversation against
    memory names/descriptions. Uses a simple LLM call (or falls back to keyword
    matching on name+description)."""
    files = list_memory_files()
    if not files:
        return []

    # Collect recent user text for context
    recent_texts = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content","")
            if isinstance(content,list):
                content = " ".join(
                    str(getattr(b,"text","")) for b in content
                    if getattr(b,"type",None) == "text"
                )
            if isinstance(content,str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
    recent = " ".join(reversed(recent_texts))[:2000]

    if not recent.strip():
        return []

    # Build catalog of name + description for LLM to choose from
    catalog_lines = []
    for i, f in enumerate(files):
        catalog_lines.append(f"{i}:{f['name']} - {f['description']}")
    catalog = "\n".join(catalog_lines)

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role":"user","content":prompt}],
            max_tokens=200,
        )
        text = response.content[0].text.strip()
        # Extract JSON array from response
        match = re.search(r'\[.*?\]',text,re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                if isinstance(idx,int) and 0<= idx < len(files):
                    selected.append(files[idx]['filename'])
                    if len(selected) >= max_items:
                        break
            return selected
    except Exception as e:
        pass

    # Fallback: keyword matching on name + description
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    return selected

def load_memories(messages:list) -> str:
    """Load relevant memory content for injection into context."""
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""

    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)

def extract_memories(messages:list):
    """Extract new memories from recent dialogue. Runs after each turn."""
    # Collect recent conversation text
    dialogue_parts = []
    for msg in messages[-10:]:
        role = msg.get("role","?")
        content = msg.get("content","")
        if isinstance(content,list):
            content = " ".join(
                str(getattr(b,"text","")) for b in content
                if getattr(b,"type",None) == "text"
            )
        if isinstance(content,str) and content.strip():
            dialogue_parts.append(f"{role}:{content}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return

    # Check existing memories to avoid duplicates
    existing = list_memory_files()
    existing_desc = "\n".join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL,messages=[{"role":"user","content":prompt}],max_tokens=800
        )
        text = response.content[0].text.strip()
        # Extract JSON array from response
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return
        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name,mem_type,desc,body)
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception as e:
        pass

CONSOLIDATE_THRESHOLD = 10

def consolidate_memories():
    """Merge duplicate/stale memories. Triggered when file count ≥ threshold."""
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return

    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL, messages=[{"role":"user","content":prompt}],max_tokens=3000
        )
        text = response.content[0].text.strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # Remove old memory files (keep MEMORY.md)
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in items:
            name = mem.get("name",f"memory_{int(time.time())}")
            mem_type = mem.get("type","user")
            desc = mem.get("description","")
            body = mem.get("body","")
            if desc and body:
                write_memory_file(name,mem_type,desc,body)

        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception as e:
        pass


# Skill catalog scan (used by build_system below)
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
        r = subprocess.run(command,shell=True,cwd=WORKDIR,
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
#  agent_loop — core: nag reminder, task auto-dispatches, run compaction pipeline before LLM, inject memories + extract after each turn
# ═══════════════════════════════════════════════════════════

round_since_todo = 0
MAX_REACTIVE_RETRIES = 1  # retry limit for reactive compact

def agent_loop(messages:list, context:dict):
    global round_since_todo
    reactive_retries = 0
    while True:
        # Re-evaluate context and prompt after each tool round
        context = update_context(context,messages)
        system = get_system_prompt(context)

        # save pre-compression snapshot for accurate memory extraction
        pre_compress = [m if isinstance(m, dict) else {"role": m.get("role",""),
            "content": str(m.get("content",""))} for m in messages]

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
                model=MODEL, system=system, messages=messages,
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
            # extract from pre-compression snapshot for full fidelity
            extract_memories(pre_compress)
            consolidate_memories()
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



# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print("Welcome to MiniClawCode!")
    print("Skill Loading — catalog in SYSTEM, content on demand")
    print("Type a question, press Enter. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except(EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q","exit",""):
            break
        trigger_hooks("UserPromptSubmit",query)
        history.append({"role":"user","content":query})
        agent_loop(history,context)
        # Print the model's final text response
        response_content = history[-1]["content"]
        if isinstance(response_content,list):
            for block in response_content:
                if getattr(block,"type",None) == "text":
                    print(block.text)
        print()

