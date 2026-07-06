from __future__ import annotations

import random

from bus.message_bus import BUS
import core.state as core_state
from models.schemas import ProtocolState


def new_request_id() -> str:
    return f'req_{random.randint(0, 999999):06d}'


def match_response(response_type: str, request_id: str, approve: bool):
    state = core_state.pending_requests.get(request_id)
    if not state:
        return
    if state.type == 'shutdown' and response_type != 'shutdown_response':
        return
    if state.type == 'plan_approval' and response_type != 'plan_approval_response':
        return
    state.status = 'approved' if approve else 'rejected'


def consume_lead_inbox(route_protocol=True) -> list[dict]:
    msgs = BUS.read_inbox('lead')
    if route_protocol:
        for msg in msgs:
            meta = msg.get('metadata', {})
            req_id = meta.get('request_id', '')
            msg_type = msg.get('type', '')
            if req_id and msg_type.endswith('_response'):
                match_response(msg_type, req_id, meta.get('approve', False))
    return msgs


def run_request_shutdown(teammate: str) -> str:
    req_id = new_request_id()
    core_state.pending_requests[req_id] = ProtocolState(
        request_id=req_id,
        type='shutdown',
        sender='lead',
        target=teammate,
        status='pending',
        payload='',
    )
    BUS.send('lead', teammate, 'Shut down.', 'shutdown_request', {'request_id': req_id})
    return f'Shutdown request sent to {teammate}'


def run_request_plan(teammate: str, task: str) -> str:
    BUS.send('lead', teammate, f'Submit plan for: {task}', 'message')
    return f'Asked {teammate} to submit a plan'


def run_review_plan(request_id: str, approve: bool, feedback: str = '') -> str:
    state = core_state.pending_requests.get(request_id)
    if not state:
        return f'Request {request_id} not found'
    state.status = 'approved' if approve else 'rejected'
    BUS.send(
        'lead',
        state.sender,
        feedback or ('Approved' if approve else 'Rejected'),
        'plan_approval_response',
        {'request_id': request_id, 'approve': approve},
    )
    return f"Plan {'approved' if approve else 'rejected'}"

