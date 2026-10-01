import unittest

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.request_limits import RequestBodyLimitMiddleware


async def echo_size(request: Request):
    body = await request.body()
    return JSONResponse({'size': len(body)})


class RequestBodyLimitTests(unittest.TestCase):
    def app(self):
        app = Starlette(routes=[
            Route('/default', echo_size, methods=['POST']),
            Route('/upload', echo_size, methods=['POST']),
        ])
        app.add_middleware(
            RequestBodyLimitMiddleware,
            default_limit=8,
            route_limits={('POST', '/upload'): 16},
            spool_threshold=4,
        )
        return app

    def test_default_limit_rejects_large_body(self):
        with TestClient(self.app()) as client:
            response = client.post('/default', content=b'123456789')
        self.assertEqual(response.status_code, 413)

    def test_route_limit_allows_bounded_upload(self):
        with TestClient(self.app()) as client:
            response = client.post('/upload', content=b'123456789')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'size': 9})
