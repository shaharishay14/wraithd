"""
The local API, as docs/api.md specifies it (the contract the web UI is built against). Every route here runs
behind daemon/security.py's Guard; handlers only do the work.
"""

import asyncio
import json
import logging
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from local_agent import __version__
from local_agent.channels.conversation import lock_for, reply
from local_agent.config import load_config
from local_agent.core import inspector
from local_agent.core.gate import approval_card
from local_agent.mcp import config as mcp_config
from local_agent.memory import notes as note_files
from local_agent.memory import vault
from local_agent.memory.notes import Note
from local_agent.schedules import describe, local_timezone
from local_agent.skills import registry
from local_agent.store import base as store_base
from local_agent.tools import builtin_tools, search_provider

logger = logging.getLogger(__name__)

MAX_RUNS = 200
PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8"><title>Local Agent</title></head>
<body style="font-family: system-ui; max-width: 40rem; margin: 4rem auto; line-height: 1.5">
<h1>Local Agent is running</h1>
<p>The web UI isn't built yet. Build it into <code>frontend/dist</code> and start the daemon with
<code>LOCAL_AGENT_UI_DIR=frontend/dist local-agent daemon</code>.</p>
<p>The API is at <code>/api</code>: see docs/api.md.</p></body></html>"""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({'error': {'code': code, 'message': message}}, status_code=status)


def stamp(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat(timespec='microseconds') if moment else None


async def body_of(request: Request) -> dict:
    try:
        data = json.loads(await request.body() or b'{}')
    except ValueError:
        raise ApiError(400, 'bad_request', 'The body is not valid JSON.') from None
    if not isinstance(data, dict):
        raise ApiError(400, 'bad_request', 'The body must be a JSON object.')
    return data


def build_api(daemon, ui_dir: Path | None) -> Starlette:
    rt = daemon.rt

    def handler(fn):
        async def wrapped(request: Request):
            try:
                return await fn(request)
            except ApiError as e:
                return error(e.status, e.code, e.message)
            except Exception:
                logger.exception(f'{request.method} {request.url.path} failed')
                return error(500, 'internal', 'Something went wrong; see the daemon\'s log.')
        return wrapped

    # --- health and status -------------------------------------------------------------------------------

    async def health(request):
        return JSONResponse({'ok': True, 'version': __version__})

    async def status(request):
        nxt = next(iter(sorted((s for s in rt.store.list_schedules() if s.next_fire_at),
                               key=lambda s: s.next_fire_at)), None)
        return JSONResponse({
            'version': __version__,
            'model': {'provider': rt.config.model.provider, 'default': rt.config.model.default,
                      'strong': rt.config.model.strong},
            'timezone': local_timezone(rt.config.timezone), 'shell_enabled': rt.config.shell_enabled,
            'search_enabled': search_provider(rt.config) is not None,
            'telegram': telegram_state(),
            'mcp': [{'name': server.name, 'connected': server.name in connected_servers(),
                     'error': daemon.mcp.errors.get(server.name) if daemon.mcp else None}
                    for server in rt.config.mcp_servers],
            'scheduler': {'running': daemon.scheduler.running,
                          'next': {'id': nxt.id, 'at': stamp(nxt.next_fire_at)} if nxt else None},
        })

    # --- chat --------------------------------------------------------------------------------------------

    async def chat(request):
        data = await body_of(request)
        message = data.get('message')
        if not isinstance(message, str) or not message.strip():
            raise ApiError(400, 'bad_request', '"message" must be a non-empty string.')
        conversation_id = data.get('conversation_id')
        if conversation_id is not None:
            conversation = rt.store.get_conversation(str(conversation_id))
            if conversation is None:
                raise ApiError(404, 'not_found', f'There is no conversation {conversation_id}.')
            if conversation.source != 'web':
                raise ApiError(409, 'conflict', f'{conversation.source} conversations are read-only here.')
        else:
            conversation = rt.store.start_conversation('web')
        if lock_for(conversation.id).locked():
            raise ApiError(409, 'conflict', 'This conversation is still answering the last message.')

        queue: asyncio.Queue = asyncio.Queue()
        names = {'run_started': 'start', 'content_delta': 'delta', 'tool_call_start': 'tool',
                 'approval_required': 'approval_required', 'approval_resolved': 'approval_resolved'}

        async def callback(event_type, payload):
            if event_type == 'tool_call_start':
                payload = {'tool': payload['tool']}
            elif event_type == 'run_started':
                payload = {'conversation_id': conversation.id, 'run_id': payload['run_id']}
            if event_type in names:
                await queue.put((names[event_type], payload))

        async def answer():
            try:
                result = await reply(rt, conversation.id, message, callback=callback, source='chat', mcp=daemon.mcp)
                await queue.put(('done', {'run_id': result.run_id, 'outcome': result.outcome,
                                          'answer': result.answer}))
            except Exception:
                logger.exception('Web chat run failed')
                await queue.put(('error', {'code': 'internal', 'message': 'The run failed; see the daemon\'s log.'}))
            # A run refused before it started (the daily budget) never sent run_started: done still carries all

        task = asyncio.create_task(answer())
        daemon.background.add(task)
        task.add_done_callback(daemon.background.discard)

        async def stream():
            while True:
                event, payload = await queue.get()
                yield sse(event, payload)
                if event in ('done', 'error'):
                    return

        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    async def conversations(request):
        return JSONResponse([{'id': c.id, 'source': c.source, 'started_at': stamp(c.started_at),
                              'last_message_at': stamp(c.last_message_at), 'preview': c.preview}
                             for c in rt.store.list_conversations()])

    async def conversation_messages(request):
        conversation_id = request.path_params['id']
        if rt.store.get_conversation(conversation_id) is None:
            raise ApiError(404, 'not_found', f'There is no conversation {conversation_id}.')
        return JSONResponse([{'id': m.id, 'role': m.role, 'content': m.content, 'created_at': stamp(m.created_at)}
                             for m in rt.store.messages(conversation_id)])

    # --- approvals ---------------------------------------------------------------------------------------

    async def approvals(request):
        wanted = request.query_params.get('status', store_base.PENDING)
        if wanted not in store_base.APPROVAL_STATUSES:
            raise ApiError(400, 'bad_request', f'status must be one of {", ".join(store_base.APPROVAL_STATUSES)}.')
        return JSONResponse([approval_card(a) for a in rt.store.list_approvals(wanted)])

    async def decide(request):
        decision = (await body_of(request)).get('decision')
        if decision not in ('approve', 'deny'):
            raise ApiError(400, 'bad_request', '"decision" must be "approve" or "deny".')
        approval_id = request.path_params['id']
        if rt.store.get_approval(approval_id) is None:
            raise ApiError(404, 'not_found', f'There is no approval {approval_id}.')
        if not rt.store.decide_approval(approval_id, store_base.APPROVED if decision == 'approve' else
                                        store_base.DENIED):
            raise ApiError(409, 'conflict', f'Already {rt.store.get_approval(approval_id).status}.')
        return JSONResponse(approval_card(rt.store.get_approval(approval_id)))

    # --- runs --------------------------------------------------------------------------------------------

    async def runs(request):
        try:
            limit = max(1, min(MAX_RUNS, int(request.query_params.get('limit', 50))))
        except ValueError:
            raise ApiError(400, 'bad_request', 'limit must be a number.') from None
        listed = []
        for info in rt.store.recent_runs(limit):
            summary = inspector.summarize(rt.store.run_events(info.run_id))
            listed.append({'run_id': info.run_id, 'started_at': stamp(info.started_at), 'source': info.source,
                           'message': info.message, **{key: summary[key] for key in (
                               'outcome', 'turns', 'tool_calls', 'tokens', 'stop_reason', 'untrusted_sources')}})
        return JSONResponse(listed)

    async def run_detail(request):
        run_id = request.path_params['id']
        events = rt.store.run_events(run_id)
        if not events:
            raise ApiError(404, 'not_found', f'There is no run {run_id}.')
        summary = inspector.summarize(events)
        summary['started_at'] = stamp(summary['started_at'])
        return JSONResponse({'run_id': run_id, 'summary': summary,
                             'events': [{**event, 'at': stamp(event['at'])} for event in inspector.timeline(events)]})

    # --- memory ------------------------------------------------------------------------------------------

    def note_path(request) -> str:
        try:
            path = vault.normalize_path(f'{request.path_params["folder"]}/{request.path_params["name"]}')
        except ValueError as e:
            raise ApiError(422, 'invalid', str(e)) from None
        return path

    def note_json(note, all_notes):
        linked_from = sorted(n.path for n in all_notes if n.path != note.path and any(
            link.removesuffix('.md') in (note.path.removesuffix('.md'), vault.stem_of(note.path)) for link in n.links))
        return {'path': note.path, 'title': note.title, 'body': note.body, 'trust': note.trust, 'source': note.source,
                'source_detail': note.source_detail, 'updated_at': stamp(note.updated_at), 'links': note.links,
                'linked_from': linked_from}

    async def memory(request):
        return JSONResponse({'folders': vault.FOLDERS,
                             'notes': [{'path': n.path, 'title': n.title, 'trust': n.trust, 'source': n.source,
                                        'updated_at': stamp(n.updated_at)} for n in rt.memory.load()]})

    async def memory_note(request):
        path = note_path(request)
        if request.method == 'GET':
            note = rt.memory.get(path)
            if note is None:
                raise ApiError(404, 'not_found', f'There is no note {path}.')
            return JSONResponse(note_json(note, rt.memory.load()))
        if request.method == 'DELETE':
            if not rt.memory.delete(path):
                raise ApiError(404, 'not_found', f'There is no note {path}.')
            return Response(status_code=204)
        data = await body_of(request)
        if not isinstance(data.get('body'), str):
            raise ApiError(400, 'bad_request', '"body" must be a string.')
        existing = rt.memory.get(path)
        # The owner's own edit vouches for the note: high trust, whatever it was
        note = Note(path=path, title=str(data.get('title') or (existing.title if existing else
                                                                note_files._default_title(path))),
                    body=data['body'], trust=vault.HIGH, source=note_files.SOURCE_USER,
                    source_detail='edited by the user in the web UI')
        rt.memory.save(note)
        return JSONResponse(note_json(rt.memory.get(path), rt.memory.load()))

    # --- skills ------------------------------------------------------------------------------------------

    def capabilities():
        names = [tool.name for tool in builtin_tools(rt, owner=True, attended=True)]
        names += [tool.name for tool in daemon.mcp.tools] if daemon.mcp else []
        return registry.capabilities(names, owner=True, provides=daemon.mcp.provides if daemon.mcp else ())

    def skill_json(skill, available, with_instructions=False):
        missing = [r for r in skill.requires if not registry.requirements_met([r], available)]
        return {'name': skill.name, 'description': skill.description, 'source': skill.source,
                'requires': list(skill.requires), 'model': skill.model, 'available': not missing, 'missing': missing,
                'files': sorted(skill.files), 'adjustments': list(skill.adjustments),
                **({'instructions': skill.instructions} if with_instructions else {})}

    def all_skills():
        by_name = {skill.name: skill for skill in registry.builtin_skills()}
        by_name.update({skill.name: skill for skill in rt.skills.user_skills()})
        rules = rt.skills.adjustments()
        return {name: replace(skill, adjustments=tuple(rules.get(name, ()))) for name, skill in sorted(by_name.items())}

    async def skills(request):
        available = capabilities()
        return JSONResponse([skill_json(skill, available) for skill in all_skills().values()])

    async def skill(request):
        found = all_skills().get(request.path_params['name'])
        if found is None:
            raise ApiError(404, 'not_found', f'There is no skill {request.path_params["name"]}.')
        return JSONResponse(skill_json(found, capabilities(), with_instructions=True))

    async def skill_adjustments(request):
        name = request.path_params['name']
        if name not in all_skills():
            raise ApiError(404, 'not_found', f'There is no skill {name}.')
        rules = (await body_of(request)).get('rules')
        if not isinstance(rules, list) or not all(isinstance(r, str) and 0 < len(r.strip()) <= 300 and '\n' not in r
                                                  for r in rules):
            raise ApiError(400, 'bad_request', '"rules" must be a list of one-line rules, each at most 300 characters.')
        rt.skills.set_adjustments(name, [' '.join(r.split()) for r in rules])
        return JSONResponse(skill_json(all_skills()[name], capabilities(), with_instructions=True))

    async def proposals(request):
        listed = []
        for folder in sorted((rt.skills.root / registry.PROPOSED).glob('*/SKILL.md')):
            try:
                meta, body = registry.parse_skill_md(folder.read_text())
            except ValueError:
                continue
            listed.append({'name': folder.parent.name, 'description': meta.get('description', ''),
                           'instructions': body, 'requires': meta.get('requires') or [],
                           'proposed_by': meta.get('proposed_by', '')})
        return JSONResponse(listed)

    def proposal_folder(request) -> Path:
        name = request.path_params['name']
        folder = rt.skills.root / registry.PROPOSED / name
        if not registry.NAME.match(name) or not (folder / 'SKILL.md').exists():
            raise ApiError(404, 'not_found', f'There is no proposal {name}.')
        return folder

    async def approve_proposal(request):
        folder = proposal_folder(request)
        if rt.skills.is_active(folder.name):
            raise ApiError(409, 'conflict', f'There is already an active skill named {folder.name}.')
        target = rt.skills.root / folder.name
        if target.exists():  # a folder with only the owner's rules for a built-in: keep them with the new skill
            for item in folder.iterdir():
                shutil.move(str(item), target / item.name)
            folder.rmdir()
        else:
            folder.rename(target)
        found = all_skills()[folder.name]
        return JSONResponse(skill_json(found, capabilities(), with_instructions=True))

    async def delete_proposal(request):
        shutil.rmtree(proposal_folder(request))
        return Response(status_code=204)

    # --- schedules ---------------------------------------------------------------------------------------

    def schedule_json(s):
        return {'id': s.id, 'message': s.message, 'cron': s.cron, 'at': s.at, 'timezone': s.timezone,
                'next_fire_at': stamp(s.next_fire_at), 'last_fired_at': stamp(s.last_fired_at), 'active': s.active,
                'when': describe(s)}

    async def schedules_list(request):
        show_all = request.query_params.get('all', 'false').lower() == 'true'
        return JSONResponse([schedule_json(s) for s in rt.store.list_schedules(active_only=not show_all)])

    async def schedule_cancel(request):
        try:
            schedule_id = int(request.path_params['id'])
        except ValueError:
            raise ApiError(404, 'not_found', 'There is no such schedule.') from None
        if not rt.store.cancel_schedule(schedule_id):
            raise ApiError(404, 'not_found', f'There is no active schedule {schedule_id}.')
        return Response(status_code=204)

    # --- MCP ---------------------------------------------------------------------------------------------

    def connected_servers() -> set[str]:
        return {server for server, _ in daemon.mcp.origin.values()} if daemon.mcp else set()

    def server_named(name):
        found = next((s for s in rt.config.mcp_servers if s.name == name), None)
        if found is None:
            raise ApiError(404, 'not_found', f'There is no MCP server {name}.')
        return found

    def server_json(s):
        return {'name': s.name, 'transport': s.transport,
                'target': ' '.join([s.command, *s.args]) if s.transport == 'stdio' else s.url,
                'provides': list(s.provides), 'always_allow': list(s.always_allow), 'secrets': list(s.env + s.headers),
                'connected': s.name in connected_servers(),
                'error': daemon.mcp.errors.get(s.name) if daemon.mcp else None}

    async def mcp_servers(request):
        return JSONResponse([server_json(s) for s in rt.config.mcp_servers])

    async def mcp_tools(request):
        server = server_named(request.path_params['name'])
        if server.name not in connected_servers():
            raise ApiError(409, 'conflict', f'{server.name} is not connected'
                           + (f': {daemon.mcp.errors[server.name]}' if daemon.mcp and server.name in daemon.mcp.errors
                              else '.'))
        listed = []
        for tool in daemon.mcp.tools:
            owner, original = daemon.mcp.origin[tool.name]
            if owner != server.name:
                continue
            tier = ('read-only' if daemon.mcp.access[tool.name].value == 'read' else
                    'always allowed' if tool.name in daemon.mcp.always_allowed else 'asks')
            listed.append({'tool': original, 'registered_name': tool.name, 'description': tool.description,
                           'tier': tier})
        return JSONResponse(listed)

    async def mcp_always_allow(request):
        server = server_named(request.path_params['name'])
        tool = request.path_params['tool']
        allow = request.method == 'PUT'
        names = [t for t in server.always_allow if t != tool] + ([tool] if allow else [])
        mcp_config.write_server(rt.home.config_path, replace(server, always_allow=tuple(names)), server.name)
        rt.config = load_config(rt.home)
        if daemon.mcp:  # the open connections follow the new setting right away
            for registered, (owner, original) in daemon.mcp.origin.items():
                if owner == server.name and original == tool and daemon.mcp.access[registered].value != 'read':
                    (daemon.mcp.always_allowed.add if allow else daemon.mcp.always_allowed.discard)(registered)
        return JSONResponse(server_json(server_named(server.name)))

    # --- Telegram ----------------------------------------------------------------------------------------

    def telegram_state():
        from local_agent.channels import telegram
        return {'configured': daemon.telegram is not None, 'paired': bool(rt.store.get_setting(telegram.OWNER_CHAT)),
                'owner': rt.store.get_setting(telegram.OWNER_NAME)}

    async def telegram_status(request):
        return JSONResponse(telegram_state())

    async def telegram_pairing(request):
        from local_agent.channels import telegram
        if daemon.telegram is None:
            raise ApiError(409, 'conflict', 'Telegram isn\'t set up: `local-agent key set telegram`, then restart the '
                                            'daemon.')
        pairing = telegram.new_pairing(rt.store)
        return JSONResponse({'code': pairing['code'], 'expires_at': pairing['expires_at']})

    # --- the UI ------------------------------------------------------------------------------------------

    async def ui(request):
        if ui_dir is None:
            return HTMLResponse(PLACEHOLDER)
        relative = request.path_params.get('path', '')
        candidate = (ui_dir / relative).resolve()
        if relative and candidate.is_relative_to(ui_dir) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(ui_dir / 'index.html')  # the app's own routes: /runs/r1, /memory...

    async def api_not_found(request):
        return error(404, 'not_found', f'There is no endpoint {request.method} {request.url.path}.')

    routes = [
        Route('/api/health', handler(health), methods=['GET']),
        Route('/api/status', handler(status), methods=['GET']),
        Route('/api/chat', handler(chat), methods=['POST']),
        Route('/api/conversations', handler(conversations), methods=['GET']),
        Route('/api/conversations/{id}/messages', handler(conversation_messages), methods=['GET']),
        Route('/api/approvals', handler(approvals), methods=['GET']),
        Route('/api/approvals/{id}', handler(decide), methods=['POST']),
        Route('/api/runs', handler(runs), methods=['GET']),
        Route('/api/runs/{id}', handler(run_detail), methods=['GET']),
        Route('/api/memory', handler(memory), methods=['GET']),
        Route('/api/memory/{folder}/{name}', handler(memory_note), methods=['GET', 'PUT', 'DELETE']),
        Route('/api/skills', handler(skills), methods=['GET']),
        Route('/api/skills/proposals', handler(proposals), methods=['GET']),
        Route('/api/skills/proposals/{name}/approve', handler(approve_proposal), methods=['POST']),
        Route('/api/skills/proposals/{name}', handler(delete_proposal), methods=['DELETE']),
        Route('/api/skills/{name}', handler(skill), methods=['GET']),
        Route('/api/skills/{name}/adjustments', handler(skill_adjustments), methods=['PUT']),
        Route('/api/schedules', handler(schedules_list), methods=['GET']),
        Route('/api/schedules/{id}', handler(schedule_cancel), methods=['DELETE']),
        Route('/api/mcp/servers', handler(mcp_servers), methods=['GET']),
        Route('/api/mcp/servers/{name}/tools', handler(mcp_tools), methods=['GET']),
        Route('/api/mcp/servers/{name}/always-allow/{tool}', handler(mcp_always_allow), methods=['PUT', 'DELETE']),
        Route('/api/telegram', handler(telegram_status), methods=['GET']),
        Route('/api/telegram/pairing', handler(telegram_pairing), methods=['POST']),
        Route('/api/{rest:path}', handler(api_not_found), methods=['GET', 'POST', 'PUT', 'DELETE']),
        Route('/', handler(ui), methods=['GET']),
        Route('/{path:path}', handler(ui), methods=['GET']),
    ]
    return Starlette(routes=routes)


def sse(event: str, payload: dict) -> str:
    return f'event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
