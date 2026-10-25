"""
The harness: everything around the model loop that the Agents SDK leaves open.

The SDK runs the loop (call the model, run tools, feed results back). The harness decides:
- what happened: every step is an event in an append-only log (events.py)
- what state the run is in: derived from the log by queries, not tracked by flags (views.py)
- when to stop: explicit conditions checked before every model call (stop.py, hooks.py, loop.py)
- what a tool may do: the permission gate and taint tracking (gate.py)
- what the model sees from a tool: a capped rendering, while the log keeps a structured one (render.py)
- what the model is reminded of, and when (reminders.py)
- how many tools it sees at once (disclosure.py)
- where it may connect, and which secrets it never sees (egress.py, secrets.py)
"""
