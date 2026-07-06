from __future__ import annotations

import re
from pathlib import Path

from core.config import MAX_MEMORY_NOTES, MEMORY_DIR, MEMORY_INDEX

CATEGORY_HINTS = {
    'user_preferences': ['preference', 'style', 'single-quotes', 'tabs', 'user'],
    'project_constraints': ['constraint', 'approval', 'deployment', 'service', 'project'],
    'architecture_notes': ['schema', 'mcp', 'agent', 'worktree', 'cron', 'architecture'],
    'recent_decisions': ['task', 'decision', 'chosen', 'confirmed', 'created'],
    'known_issues': ['issue', 'error', 'warning', 'fix', 'broken'],
}


def parse_memory_note(raw: str) -> tuple[dict, str]:
    if not raw.startswith('---\n'):
        return {}, raw.strip()
    parts = raw.split('---\n', 2)
    if len(parts) < 3:
        return {}, raw.strip()
    meta_block = parts[1]
    body = parts[2].strip()
    metadata = {}
    for line in meta_block.splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        metadata[key.strip()] = value.strip()
    return metadata, body


def classify_memory_note(path: Path, body: str) -> str:
    haystack = f'{path.stem}\n{body}'.lower()
    for category, hints in CATEGORY_HINTS.items():
        if any(hint in haystack for hint in hints):
            return category
    return 'recent_decisions'


def load_memory_notes() -> list[dict]:
    notes = []
    if not MEMORY_DIR.exists():
        return notes
    for path in sorted(MEMORY_DIR.glob('*.md')):
        if path.name == MEMORY_INDEX.name:
            continue
        try:
            raw = path.read_text(encoding='utf-8')
        except Exception:
            continue
        metadata, body = parse_memory_note(raw)
        notes.append(
            {
                'name': path.stem,
                'category': classify_memory_note(path, body),
                'body': body.strip(),
                'description': metadata.get('description', ''),
                'type': metadata.get('type', ''),
                'path': str(path),
                'mtime': path.stat().st_mtime,
            }
        )
    return notes


def select_relevant_memories(query: str, limit_chars: int = 1800) -> str:
    query_lower = (query or '').lower()
    selected = []
    used = 0
    notes = load_memory_notes()
    if not notes and MEMORY_INDEX.exists():
        return MEMORY_INDEX.read_text(encoding='utf-8')[:limit_chars]

    def score(note: dict) -> int:
        haystack = f"{note['name']} {note['body']}".lower()
        tokens = [token for token in query_lower.replace('/', ' ').replace('-', ' ').split() if len(token) > 2]
        return sum(1 for token in tokens if token in haystack)

    ranked = sorted(notes, key=lambda note: (score(note), note['category'], note['name']), reverse=True)
    for note in ranked:
        excerpt = f"[{note['category']}] {note['name']}\n{note['body'][:400]}".strip()
        if score(note) <= 0 and selected:
            continue
        if used + len(excerpt) > limit_chars:
            break
        selected.append(excerpt)
        used += len(excerpt) + 2

    if not selected and MEMORY_INDEX.exists():
        return MEMORY_INDEX.read_text(encoding='utf-8')[:limit_chars]
    return '\n\n'.join(selected)


def slugify_memory_name(name: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', (name or '').strip().lower()).strip('-')
    return slug[:80] or 'memory-note'


def write_memory_note(name: str, description: str, body: str, note_type: str = 'fact') -> Path:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify_memory_name(name)
    path = MEMORY_DIR / f'{slug}.md'
    content = (
        '---\n'
        f'name:{slug}\n'
        f'description:{description.strip()}\n'
        f'type:{note_type.strip()}\n'
        '---\n\n'
        f'{body.strip()}\n'
    )
    path.write_text(content, encoding='utf-8')
    return path


def refresh_memory_index():
    notes = load_memory_notes()
    lines = []
    for note in notes:
        summary = note.get('description') or (note['body'].splitlines()[0][:120] if note.get('body') else '')
        if summary:
            lines.append(f"- [{note['name']}]({note['name']}.md) - {summary}")
    MEMORY_INDEX.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')


def remember(name: str, description: str, body: str, note_type: str = 'fact') -> Path:
    path = write_memory_note(name, description, body, note_type=note_type)
    refresh_memory_index()
    return path


def list_memory_note_names() -> str:
    notes = load_memory_notes()
    if not notes:
        return 'No memory notes.'
    return '\n'.join(f"  {note['name']} [{note['category']}]" for note in notes)


def dedupe_memory_notes() -> str:
    notes = load_memory_notes()
    seen: dict[str, dict] = {}
    removed = 0
    for note in sorted(notes, key=lambda item: (item.get('mtime', 0), item['name']), reverse=True):
        body_key = re.sub(r'\s+', ' ', note['body']).strip().lower()
        if body_key in seen:
            Path(note['path']).unlink(missing_ok=True)
            removed += 1
            continue
        seen[body_key] = note
    refresh_memory_index()
    return f'Deduplicated memory notes: removed {removed}, kept {len(seen)}'


def prune_memory_notes(max_notes: int = MAX_MEMORY_NOTES) -> str:
    notes = load_memory_notes()
    if len(notes) <= max_notes:
        return f'No pruning needed. {len(notes)} note(s) present.'
    kept = sorted(notes, key=lambda item: (item.get('mtime', 0), item['name']), reverse=True)[:max_notes]
    keep_paths = {note['path'] for note in kept}
    removed = 0
    for note in notes:
        if note['path'] not in keep_paths:
            Path(note['path']).unlink(missing_ok=True)
            removed += 1
    refresh_memory_index()
    return f'Pruned memory notes: removed {removed}, kept {len(kept)}'
