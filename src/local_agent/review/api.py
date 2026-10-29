"""
The review queue in the local API (docs/api.md, "Review queue"):

    GET  /api/review?status=pending|approved|rejected|all&kind=memory
    GET  /api/review/{id}
    POST /api/review/{id}/approve
    POST /api/review/{id}/reject        {"reason": "..."} (optional)
"""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from local_agent.review import queue


def item_json(item: queue.ReviewItem, detail: bool = False) -> dict:
    found = {'id': item.id, 'kind': item.kind, 'summary': item.summary, 'source': item.source, 'run_id': item.run_id,
             'taint': item.taint, 'status': item.status, 'created_at': item.created_at.isoformat(),
             'decided_at': item.decided_at.isoformat() if item.decided_at else None, 'decided_by': item.decided_by,
             'result': item.result}
    if detail:
        found |= {'content': item.content, 'detail': queue.describe(item)}
    return found


def routes(daemon, handler) -> list[Route]:
    from local_agent.daemon.api import ApiError, body_of  # the API module imports this one inside its block

    rt = daemon.rt

    def item_id(request: Request) -> int:
        try:
            return int(request.path_params['id'])
        except ValueError:
            raise ApiError(404, 'not_found', 'There is no such review item.') from None

    async def review_list(request: Request):
        status = request.query_params.get('status', queue.PENDING)
        if status not in (*queue.STATUSES, 'all'):
            raise ApiError(400, 'bad_request', f'status is one of {", ".join(queue.STATUSES)} or all.')
        items = queue.ReviewQueue(rt.store).list(status, request.query_params.get('kind') or None)
        return JSONResponse([item_json(item) for item in items])

    async def review_item(request: Request):
        item = queue.ReviewQueue(rt.store).get(item_id(request))
        if item is None:
            raise ApiError(404, 'not_found', 'There is no such review item.')
        return JSONResponse(item_json(item, detail=True))

    def decided(decide):
        async def endpoint(request: Request):
            number = item_id(request)
            reason = str((await body_of(request)).get('reason') or '') if decide is queue.reject else ''
            try:
                item = (decide(rt.store, rt.home, number, 'api', reason) if decide is queue.reject else
                        decide(rt.store, rt.home, number, 'api'))
            except queue.NotFound as e:
                raise ApiError(404, 'not_found', str(e)) from None
            except queue.ReviewError as e:
                raise ApiError(409, 'conflict', str(e)) from None
            return JSONResponse(item_json(item, detail=True))
        return endpoint

    return [
        Route('/api/review', handler(review_list), methods=['GET']),
        Route('/api/review/{id}', handler(review_item), methods=['GET']),
        Route('/api/review/{id}/approve', handler(decided(queue.approve)), methods=['POST']),
        Route('/api/review/{id}/reject', handler(decided(queue.reject)), methods=['POST']),
    ]
