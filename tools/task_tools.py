from __future__ import annotations

import json
import random
import time
from dataclasses import asdict

from context.memory_store import remember
from core.config import TASKS_DIR, print_event
from models.schemas import Task


def _task_path(task_id: str):
    return TASKS_DIR / f'{task_id}.json'


def create_task(subject: str, description: str = '', blockedBy: list[str] | None = None) -> Task:
    task = Task(
        id=f'task_{int(time.time())}_{random.randint(0, 9999):04d}',
        subject=subject,
        description=description,
        status='pending',
        owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    remember(
        f'task-{task.id}',
        f'Task created: {task.subject}',
        f'Task {task.id} created.\nSubject: {task.subject}\nDescription: {task.description or "(none)"}\nBlocked by: {task.blockedBy or []}',
        note_type='event',
    )
    return task


def save_task(task: Task):
    task.updated_at = time.time()
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2), encoding='utf-8')


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text(encoding='utf-8')))


def list_tasks() -> list[Task]:
    return [Task(**json.loads(p.read_text(encoding='utf-8'))) for p in sorted(TASKS_DIR.glob('task_*.json'))]


def get_task_json(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), indent=2)


def can_start(task_id: str) -> bool:
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != 'completed':
            return False
    return True


def claim_task(task_id: str, owner: str = 'agent') -> str:
    task = load_task(task_id)
    if task.status != 'pending':
        return f'Task {task_id} is {task.status}, cannot claim'
    if task.owner:
        return f'Task {task_id} already owned by {task.owner}'
    if not can_start(task_id):
        deps = [d for d in task.blockedBy if _task_path(d).exists() and load_task(d).status != 'completed']
        missing = [d for d in task.blockedBy if not _task_path(d).exists()]
        parts = []
        if deps:
            parts.append(f'blocked by: {deps}')
        if missing:
            parts.append(f'missing deps: {missing}')
        return 'Cannot start - ' + ', '.join(parts)
    task.owner = owner
    task.status = 'in_progress'
    save_task(task)
    remember(
        f'task-claim-{task.id}',
        f'Task claimed by {owner}',
        f'Task {task.id} ({task.subject}) is now in progress.\nOwner: {owner}',
        note_type='event',
    )
    print_event('task', f'{task.subject} -> in_progress', 81)
    return f'Claimed {task.id} ({task.subject})'


def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != 'in_progress':
        return f'Task {task_id} is {task.status}, cannot complete'
    task.status = 'completed'
    save_task(task)
    remember(
        f'task-complete-{task.id}',
        f'Task completed: {task.subject}',
        f'Task {task.id} ({task.subject}) completed.\nOwner: {task.owner or "(unknown)"}',
        note_type='event',
    )
    unblocked = [t.subject for t in list_tasks() if t.status == 'pending' and t.blockedBy and can_start(t.id)]
    print_event('task', f'{task.subject} -> completed', 114)
    msg = f'Completed {task.id} ({task.subject})'
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    return msg


def requeue_task(task_id: str, reason: str = '') -> str:
    task = load_task(task_id)
    if task.status not in ('in_progress', 'failed'):
        return f'Task {task_id} is {task.status}, cannot requeue'
    task.status = 'pending'
    task.owner = None
    task.attempts += 1
    task.last_error = reason
    save_task(task)
    remember(
        f'task-requeue-{task.id}',
        f'Task requeued: {task.subject}',
        f'Task {task.id} ({task.subject}) was requeued.\nReason: {reason or "(none)"}\nAttempts: {task.attempts}',
        note_type='event',
    )
    print_event('task', f'{task.subject} -> requeued', 179)
    return f'Requeued {task.id} ({task.subject})'


def scan_unclaimed_tasks() -> list[dict]:
    unclaimed = []
    for f in sorted(TASKS_DIR.glob('task_*.json')):
        task = json.loads(f.read_text(encoding='utf-8'))
        if task.get('status') == 'pending' and not task.get('owner') and can_start(task['id']):
            unclaimed.append(task)
    return unclaimed


def run_create_task(subject: str, description: str = '', blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ''
    print_event('task', f'created {task.subject}{deps}', 75)
    return f'Created {task.id}: {task.subject}{deps}'


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return 'No tasks.'
    return '\n'.join(
        f'  {t.id}: {t.subject} [{t.status}]'
        + (f' owner={t.owner}' if t.owner else '')
        + (f' attempts={t.attempts}' if t.attempts else '')
        + (f' (wt:{t.worktree})' if t.worktree else '')
        + (f' error={t.last_error[:40]}' if t.last_error else '')
        for t in tasks
    )


def run_get_task(task_id: str) -> str:
    try:
        return get_task_json(task_id)
    except FileNotFoundError:
        return f'Error: task {task_id} not found'


def run_claim_task(task_id: str) -> str:
    try:
        return claim_task(task_id, owner='agent')
    except FileNotFoundError:
        return f'Error: task {task_id} not found'


def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id)
    except FileNotFoundError:
        return f'Error: task {task_id} not found'


def run_requeue_task(task_id: str, reason: str = '') -> str:
    try:
        return requeue_task(task_id, reason)
    except FileNotFoundError:
        return f'Error: task {task_id} not found'


def auto_plan_tasks(objective: str, include_review: bool = True, include_test: bool = True) -> list[Task]:
    plan_specs = [
        {
            'subject': f'Plan: {objective[:60]}',
            'description': f'Create an implementation plan for: {objective}',
        },
        {
            'subject': f'Implement: {objective[:60]}',
            'description': f'Implement the requested work for: {objective}',
        },
    ]
    if include_review:
        plan_specs.append(
            {
                'subject': f'Review: {objective[:60]}',
                'description': f'Review the implementation for: {objective}',
            }
        )
    if include_test:
        plan_specs.append(
            {
                'subject': f'Test: {objective[:60]}',
                'description': f'Run validation and tests for: {objective}',
            }
        )

    created: list[Task] = []
    previous_task_id: str | None = None
    for spec in plan_specs:
        blocked_by = [previous_task_id] if previous_task_id else None
        task = create_task(spec['subject'], spec['description'], blocked_by)
        created.append(task)
        previous_task_id = task.id
    return created
