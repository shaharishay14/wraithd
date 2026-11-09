#!/usr/bin/env python3
"""
A stand-in for the `claude` program, for tests that run the real claude_agent_sdk: it speaks just enough of the
stream-json protocol (the initialize handshake, an init message, one result) and calls no model. FAKE_CLAUDE_MODE
'grandchild' leaves a child holding its stdout open for a moment after it exits.
"""

import json
import os
import sys

MODE = os.environ.get('FAKE_CLAUDE_MODE', '')


def out(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + '\n')
    sys.stdout.flush()


def main() -> None:
    if '-v' in sys.argv or '--version' in sys.argv:
        print('2.1.300 (Claude Code)')
        return
    if 'grandchild' in MODE:
        os.posix_spawn(sys.executable, [sys.executable, '-c', 'import time; time.sleep(2)'], os.environ)
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        if message.get('type') == 'control_request':
            out({'type': 'control_response',
                 'response': {'subtype': 'success', 'request_id': message['request_id'], 'response': {}}})
        elif message.get('type') == 'user':
            out({'type': 'system', 'subtype': 'init', 'apiKeySource': 'none', 'session_id': 's'})
            out({'type': 'result', 'subtype': 'success', 'duration_ms': 1, 'duration_api_ms': 1, 'is_error': False,
                 'num_turns': 1, 'session_id': 's', 'result': 'OK',
                 'usage': {'input_tokens': 3, 'output_tokens': 1}})


if __name__ == '__main__':
    main()
