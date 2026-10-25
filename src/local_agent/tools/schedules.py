"""
The schedule tools: create (behind approval, since a schedule later runs on its own), list and cancel. The model
turns "every weekday at 8" into a cron and "remind me Friday at 9" into an ISO time; the harness checks both and
reads them in the owner's timezone.
"""

from datetime import UTC, datetime

from local_agent import schedules
from local_agent.tools.base import function_tool, tool_error

CREATE, LIST, CANCEL = 'schedule_create', 'schedule_list', 'schedule_cancel'
NAMES = (CREATE, LIST, CANCEL)
# The daemon fires schedules: the agent says so, so the owner knows to keep it running
WHEN_IT_RUNS = 'It runs while Local Agent\'s daemon is running (`local-agent daemon`), and the answer is sent to them.'


def _shown(schedule) -> dict:
    return {'id': schedule.id, 'message': schedule.message, 'when': schedules.describe(schedule)}


def schedule_tools(store, timezone: str, clock=lambda: datetime.now(UTC)) -> list:
    async def create(message: str, cron: str = '', at: str = '') -> dict:
        try:
            schedule = schedules.create(store, message, timezone, clock(), cron=cron or None, at=at or None)
        except schedules.ScheduleError as e:
            return tool_error(str(e), 'Fix the schedule and try again, or ask the user what they meant.')
        return {'created': _shown(schedule),
                'hint': f'Tell the user it is saved and when it is due. {WHEN_IT_RUNS} They can see and remove '
                        'schedules with `local-agent schedules list` and `local-agent schedules rm`.'}

    async def list_all() -> dict:
        return {'schedules': [_shown(schedule) for schedule in store.list_schedules()], 'timezone': timezone}

    async def cancel(id: int) -> dict:
        try:
            schedule_id = int(id)
        except (TypeError, ValueError):
            return tool_error(f'{id!r} is not a schedule id.', f'Call {LIST} to see the ids.')
        if not store.cancel_schedule(schedule_id):
            return tool_error(f'There is no active schedule {schedule_id}.', f'Call {LIST} to see the ids.')
        return {'cancelled': schedule_id}

    now_local = f'The user\'s timezone is {timezone}.'
    return [
        function_tool(
            CREATE,
            ('Schedule a message for the agent to run later on its own: repeating with a 5-field cron ("0 8 * * 1-5" '
             'is 8:00 on weekdays), or once with an ISO time ("2026-11-06T09:00"). Both are read in the user\'s '
             f'timezone. {now_local} Use it for "every weekday at 8..." and "remind me Friday...". The user is asked '
             'to approve it.'),
            {'properties': {
                'message': {'type': 'string', 'description': 'What to do when it runs, as an instruction, e.g. '
                                                             '"Remind me to call the bank"'},
                'cron': {'type': 'string', 'description': 'For a repeating schedule'},
                'at': {'type': 'string', 'description': 'For a one-off, an ISO local time'}},
             'required': ['message']},
            create),
        function_tool(LIST, f'List the active schedules and reminders, with their ids and next times. {now_local}',
                      {'properties': {}}, list_all),
        function_tool(CANCEL, 'Cancel a schedule or reminder by its id (from schedule_list).',
                      {'properties': {'id': {'type': 'integer', 'description': 'The schedule id'}}, 'required': ['id']},
                      cancel),
    ]
