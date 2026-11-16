"""Optional Goal input routing; ordinary tasks keep their existing author checks."""
from __future__ import annotations

import logging

from .ide_goal import _notice, _request

log = logging.getLogger(__name__)


async def preempt_current(rt, message, sid, conversation_key, running_tasks, text, attachments, *, is_dm):
    """Return True once a Goal mutation was attempted; never redeliver ambiguous input."""
    try:
        data = await _request(rt, 'GET', '/v1/plugins/ide/goal', params={'session_id': sid})
        if (not isinstance(data, dict) or data.get('session_id') != sid or data.get('is_child') is not False
                or data.get('status') not in {'running', 'waiting'}
                or not isinstance(data.get('goal_id'), str) or not data['goal_id']):
            return False
        execution = data.get('execution')
        target = data.get('preempt_target')
        if not isinstance(execution, dict) or not execution.get('task_id') or not isinstance(target, dict):
            return False
        tid, eid, seq = target.get('task_id'), target.get('execution_id'), target.get('source_inbound_seq')
        if (not isinstance(tid, str) or not tid or not isinstance(eid, str) or not eid
                or type(seq) is not int or seq <= 0 or target.get('session_id') != sid
                or target.get('conversation_key') != conversation_key):
            return False
        matches = [task for task in running_tasks if task.task_id == tid]
        if len(matches) != 1 or matches[0].source_inbound_seq != seq:
            return False
        # Goal identity permits missing/background triggers, not taking over somebody's
        # ordinary author-owned request that later joined the Goal execution chain.
        trigger = rt.session_state.get_trigger(seq)
        if trigger:
            if trigger.session_id != sid or trigger.conversation_key != conversation_key:
                return False
            if not is_dm and not trigger.platform_data.get('background'):
                origin = trigger.platform_data.get('origin_message') or trigger.platform_data.get('message')
                if origin and origin.author.id != message.author.id:
                    return False
        if (rt.session_state.get_session_id(conversation_key) != sid
                or conversation_key != f'discord:{message.channel.id}'):
            return False
    except Exception:
        # Optional plugin unavailable or malformed GET: nothing has been delivered.
        log.debug('Goal preempt identity unavailable', exc_info=True)
        return False

    body = {'goal_id': data['goal_id'], 'expected_task_id': tid, 'expected_execution_id': eid,
            'expected_conversation_key': conversation_key,
            'request_id': f'discord-input:{message.channel.id}:{message.id}',
            'message': text, 'attachments': attachments or []}
    notice = '追加指令已交给当前 Goal 任务。'
    try:
        result = await _request(rt, 'POST', '/v1/plugins/ide/goal/preempt',
                                params={'session_id': sid}, json=body)
        if (not isinstance(result, dict) or result.get('ok') is not True
                or result.get('session_id') != sid or result.get('task_id') != tid
                or result.get('source_inbound_seq') != seq):
            notice = '追加结果未确认，请核对任务状态；本次不会另建任务或自动重发。'
    except Exception as exc:
        response = getattr(exc, 'response', None)
        notice = ('当前 Goal 执行已变化，本次未另建任务；请核对状态后重新发送。'
                  if getattr(response, 'status_code', None) == 409 else
                  '追加结果未确认，请核对任务状态；本次不会另建任务或自动重发。')
        log.warning('Goal input preempt not confirmed', exc_info=True)
    try:
        # Do not replace the background trigger, progress card, or cancellation owner.
        await _notice(rt, message, notice)
    except Exception:
        log.warning('Goal input acknowledgement could not be sent', exc_info=True)
    return True
