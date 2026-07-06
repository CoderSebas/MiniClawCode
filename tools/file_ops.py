from __future__ import annotations

from pathlib import Path

from core.config import WORKDIR


def safe_path(p: str, cwd: Path = None) -> Path:
    base = cwd or WORKDIR
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f'Path escapes workspace: {p}')
    return path


def run_read(path: str, limit: int | None = None, offset: int = 0, cwd: Path = None) -> str:
    try:
        lines = safe_path(path, cwd).read_text(encoding='utf-8').splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f'... ({len(lines) - limit} more lines)']
        return '\n'.join(lines)
    except Exception as e:
        return f'Error: {e}'


def run_write(path: str, content: str, cwd: Path = None) -> str:
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding='utf-8')
        return f'Wrote {len(content)} bytes to {path}'
    except Exception as e:
        return f'Error: {e}'


def run_edit(path: str, old_text: str, new_text: str, cwd: Path = None) -> str:
    try:
        fp = safe_path(path, cwd)
        text = fp.read_text(encoding='utf-8')
        if old_text not in text:
            return f'Error: text not found in {path}'
        fp.write_text(text.replace(old_text, new_text, 1), encoding='utf-8')
        return f'Edited {path}'
    except Exception as e:
        return f'Error: {e}'


def run_glob(pattern: str, cwd: Path = None) -> str:
    import glob as g

    try:
        base = cwd or WORKDIR
        results = []
        for match in g.glob(pattern, root_dir=base):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return '\n'.join(results) if results else '(no matches)'
    except Exception as e:
        return f'Error: {e}'
