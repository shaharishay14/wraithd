"""`python -m local_agent`: the same as the `local-agent` command (the background service runs it this way)."""

from local_agent.cli import main

raise SystemExit(main())
