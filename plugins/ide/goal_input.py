"""Authenticated input to an exact active Goal execution, without replacing its task."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from supervisor.admin_api import verify_admin_token


def _deliver(service, sid: str, body: dict) -> dict:
    from .goal import ACTIVE

    with service.runtime.transaction():
        data = service.read(sid)
        if data['session_id'] != sid or data.get('is_child'):
            raise HTTPException(403, 'Goal input requires its owning session.')
        if data['goal_id'] != body['goal_id'] or data['status'] not in ACTIVE:
            raise HTTPException(409, 'Goal identity or execution changed.')
        target = data.get('preempt_target') or {}
        if (target.get('task_id') != body['expected_task_id']
                or target.get('execution_id') != body['expected_execution_id']
                or target.get('conversation_key') != body['expected_conversation_key']
                or target.get('session_id') != sid):
            raise HTTPException(409, 'Goal execution changed; input was not rerouted.')
        try:
            accepted = service.runtime.preempt_execution(
                sid, target['task_id'], target['execution_id'], body['message'], body['request_id'],
                roots=service.documents[sid]['_roots'], source='user', attachments=body.get('attachments', []),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if not accepted:
            raise HTTPException(409, 'Goal execution ended or changed.')
        return {'ok': True, 'session_id': sid, 'task_id': target['task_id'],
                'source_inbound_seq': target['source_inbound_seq']}


def add_routes(router: APIRouter) -> None:
    @router.post('/goal/preempt')
    async def preempt_goal(request: Request, session_id: str):
        from .goal import _service

        verify_admin_token(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 256 * 1024:
                raise HTTPException(413, 'Goal input too large.')
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(422, 'Invalid JSON.') from None
        required = {'goal_id', 'expected_task_id', 'expected_execution_id',
                    'expected_conversation_key', 'request_id', 'message'}
        if not isinstance(body, dict) or set(body) - required - {'attachments'} or not required <= set(body):
            raise HTTPException(422, 'Invalid Goal input fields.')
        for key in required:
            limit = 128 * 1024 if key == 'message' else 300
            if not isinstance(body[key], str) or not body[key].strip() or len(body[key]) > limit:
                raise HTTPException(422, f'Invalid {key}.')
        attachments = body.get('attachments', [])
        if not isinstance(attachments, list) or len(attachments) > 100 or any(not isinstance(a, dict) for a in attachments):
            raise HTTPException(422, 'Invalid attachments.')
        try:
            return await asyncio.to_thread(_deliver, _service(request), session_id, body)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
