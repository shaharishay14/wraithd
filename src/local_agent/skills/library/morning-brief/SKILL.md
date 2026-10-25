---
name: morning-brief
description: >-
  Today's agenda, what needs a reply, and anything urgent, in one short message. Use for "morning brief", "what's my day", "catch me up".
requires: [GOOGLECALENDAR|OUTLOOK, GMAIL|OUTLOOK]
---
1. Calendar: today's events from now until the end of the day, in the user's timezone.
2. Email: unread from the last 24 hours, newest first, at most 30. Skip newsletters, receipts and notifications unless one is clearly urgent.
3. If memory is available, search it for the people and projects that came up, to say who someone is in a few words.
4. Weather only if the agent has it and an event is outdoors or the user travels today.

Write it as:
- **Today**: one line per event, `09:30 Design review (Dana, Omer)`. Flag overlaps and back-to-back runs.
- **Needs you**: emails waiting for a decision or reply from the user, one line each with the sender and the ask.
- **FYI**: at most 3 lines, only if worth it.

Rules:
- Under 15 lines. No greeting, no sign-off.
- Never reply, archive or label anything: this is read-only.
- Nothing on the calendar or nothing urgent: say so in one line, don't pad.
