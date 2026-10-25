"""
notify_owner: the agent messages its owner, on whatever channel reaches them. It's how a scheduled run delivers
its answer, and how any run can say "done" or "this needs you". The message only ever goes to the owner, so it
needs no approval, and it works unattended.

Where it goes is the runtime's notifier: Telegram when the daemon has a paired owner chat, otherwise the
"Notifications" conversation in the store, which the web UI lists like any other.
"""

from local_agent.tools.base import function_tool, tool_error

NOTIFY = 'notify_owner'
MAX_CHARS = 3500  # Telegram's limit is 4096; leave room for the header


class StoreNotifier:
    """Keeps notifications in one conversation (source 'notify'), so nothing is lost without a channel."""

    channel = 'inbox'

    def __init__(self, store):
        self.store = store

    async def deliver(self, text: str) -> str:
        conversation = self.store.find_conversation('notify', 'inbox') or self.store.start_conversation(
            'notify', external='inbox')
        self.store.add_message(conversation.id, 'assistant', text)
        return self.channel


def notify_tool(notifier):
    async def notify(message: str) -> dict:
        message = (message or '').strip()
        if not message:
            return tool_error('The message is empty.', 'Write what the user should know.')
        delivered = await notifier.deliver(message[:MAX_CHARS])
        return {'delivered': True, 'channel': delivered}

    return function_tool(
        NOTIFY,
        ('Send the user a message on their own channel (Telegram, or their inbox), e.g. the result of a scheduled '
         'task, or that something needs them. Only the user receives it.'),
        {'properties': {'message': {'type': 'string', 'description': 'What to tell the user, in plain text'}},
         'required': ['message']},
        notify)
