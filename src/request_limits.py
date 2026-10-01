"""Bounded ASGI request body buffering with route-specific limits."""

from tempfile import SpooledTemporaryFile

from starlette.responses import JSONResponse


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app,
        *,
        default_limit,
        route_limits=None,
        spool_threshold=1024 * 1024,
    ):
        self.app = app
        self.default_limit = max(int(default_limit), 1)
        self.route_limits = {
            (str(method).upper(), str(path)): max(int(limit), 1)
            for (method, path), limit in (route_limits or {}).items()
        }
        self.spool_threshold = max(int(spool_threshold), 64 * 1024)

    def _limit(self, scope):
        return self.route_limits.get(
            (str(scope.get('method') or '').upper(), scope.get('path') or ''),
            self.default_limit,
        )

    async def _reject(self, scope, receive, send, status_code, detail):
        response = JSONResponse(status_code=status_code, content={'detail': detail})
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        if str(scope.get('method') or '').upper() in {'GET', 'HEAD', 'OPTIONS'}:
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope)
        headers = {
            key.lower(): value
            for key, value in scope.get('headers') or ()
        }
        raw_length = headers.get(b'content-length')
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await self._reject(
                    scope, receive, send, 400, 'invalid Content-Length'
                )
                return
            if content_length < 0:
                await self._reject(
                    scope, receive, send, 400, 'invalid Content-Length'
                )
                return
            if content_length > limit:
                await self._reject(
                    scope, receive, send, 413, 'request body exceeds size limit'
                )
                return
        with SpooledTemporaryFile(max_size=min(limit, self.spool_threshold)) as body:
            total = 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                total += len(chunk)
                if total > limit:
                    await self._reject(
                        scope, receive, send, 413,
                        'request body exceeds size limit',
                    )
                    return
                body.write(chunk)
                if not message.get('more_body', False):
                    break
            body.seek(0)
            finished = False

            async def replay():
                nonlocal finished
                if finished:
                    return await receive()
                chunk = body.read(64 * 1024)
                if chunk:
                    more_body = body.tell() < total
                    if not more_body:
                        finished = True
                    return {
                        'type': 'http.request',
                        'body': chunk,
                        'more_body': more_body,
                    }
                finished = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}

            await self.app(scope, replay, send)
