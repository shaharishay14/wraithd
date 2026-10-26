# Compaction

**Files:** `core/compaction.py`, `channels/conversation.py`, `channels/terminal.py`

## One-liner

> A long conversation keeps its newest 10 messages word for word and folds older ones into a rolling summary,
> stored with the conversation, so the context stays small without the agent forgetting what was decided.

## How it works

- **Before each run**, `history_for(store, conversation_id, complete)` builds the history the model gets:
  the summary (if any), older messages not folded yet, and the newest 10 messages.
- **Folding happens in batches.** Once 10 older messages pile up, one tool-less model call (`Runtime.complete`)
  updates the summary: keep facts, names, decisions, preferences and open tasks; drop small talk; at most 200
  words. The conversation row records the summary and the last message it covers (`summarized_until`).
- **It's logged between runs** as a `compaction` event (conversation, messages folded, summary size), shown by
  `local-agent events` and `GET /api/events`.
- **A failed summary call changes nothing:** the old summary stays and the messages go to the model as they
  are, to be folded on a later turn.
- **The summary goes in as a user message, not a system one.** It's built from the conversation's content, so
  it gets user-level trust, and a summarized earlier email can't speak with the harness's voice. When history
  was summarized and the owner refers back to something, a reminder tells the model that details may be missing
  and to ask or search memory ([Reminders](reminders.md)).

## On a laptop

agent-studio kept the summary on the conversation row in Postgres. Here it's on the conversation in the SQLite
store, so the terminal and the daemon (web UI, Telegram) see the same summary, and it survives a restart.
Distill (turning a conversation into memory notes) is separate: it runs when a terminal session ends and
hourly in the daemon ([Memory](memory.md)).

## Trade-offs

- The summary is lossy. A detail older than the window survives only if the summary kept it or distill saved it
  to memory.
- Compaction tokens are the owner's too. They go through the same model, but don't count toward the daily
  budget yet.
