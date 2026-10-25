"""
Chat history compaction: the newest messages stay word for word, older ones are folded into a rolling summary.
The summary is stored with the conversation, so it survives a restart and the daemon and the terminal see the
same one.
"""

import logging

from local_agent.core import events as ev

logger = logging.getLogger(__name__)

RECENT_MESSAGES_VERBATIM = 10
COMPACT_BATCH_SIZE = 10  # update the summary once this many older messages pile up
SUMMARY_LABEL = '[Summary of the earlier conversation, for context]'


async def history_for(store, conversation_id: str, complete, exclude_latest: bool = True) -> list[dict]:
    """
    The conversation as the model gets it: [summary] + older messages not summarized yet + the recent window.
    exclude_latest leaves out the newest message (the one this run answers, which run() adds itself).
    complete(prompt) -> text makes the summary call; if it fails, the old summary stays and the messages are
    sent as they are, to be folded on a later turn.
    """
    conversation = store.get_conversation(conversation_id)
    messages = store.messages(conversation_id, after=conversation.summarized_until)
    if exclude_latest and messages:
        messages = messages[:-1]
    older, recent = messages[:-RECENT_MESSAGES_VERBATIM] or [], messages[-RECENT_MESSAGES_VERBATIM:]
    if len(messages) <= RECENT_MESSAGES_VERBATIM:
        older, recent = [], messages

    summary, pending = conversation.summary, older
    if len(pending) >= COMPACT_BATCH_SIZE:
        try:
            summary = await summarize(complete, summary, pending)
            store.update_conversation(conversation_id, summary=summary, summarized_until=pending[-1].id)
            ev.record_event(store, ev.COMPACTION, conversation=conversation_id, messages_folded=len(pending),
                            summary_chars=len(summary))
            pending = []
        except Exception as e:
            logger.warning(f'Chat history compaction failed: {type(e).__name__}')

    compacted = []
    if summary:
        # Role "user", not "system": the summary is built from user content, so it gets user-level trust
        compacted.append({'role': 'user', 'content': f'{SUMMARY_LABEL}\n{summary}'})
    return compacted + [{'role': m.role, 'content': m.content} for m in pending + recent]


def is_summarized(history: list[dict]) -> bool:
    return any(str(item.get('content', '')).startswith(SUMMARY_LABEL) for item in history)


async def summarize(complete, previous_summary: str, messages) -> str:
    """Folds new messages into the existing summary with one short model call."""
    transcript = '\n'.join(f'{m.role}: {m.content[:2000]}' for m in messages)
    prompt = (
        'Update the summary of this conversation. Keep facts, names, decisions, user preferences '
        'and open tasks. Drop small talk. Maximum 200 words.\n\n'
        f'Current summary:\n{previous_summary or "(none)"}\n\n'
        f'New messages:\n{transcript}'
    )
    return (await complete(prompt)).strip() or previous_summary
