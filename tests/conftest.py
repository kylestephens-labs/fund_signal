import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app


class _SyncASGIClient:
    """Minimal synchronous wrapper around httpx.AsyncClient for ASGI apps."""

    def __init__(self, app):
        transport = httpx.ASGITransport(app=app)
        self._client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

    def request(self, method: str, url: str, **kwargs):
        return asyncio.run(self._client.request(method, url, **kwargs))

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def close(self) -> None:
        asyncio.run(self._client.aclose())


@pytest.fixture
def client():
    """Create test client compatible with older/newer httpx releases."""
    try:
        test_client = TestClient(app)
        yield test_client
    except TypeError:
        fallback_client = _SyncASGIClient(app)
        try:
            yield fallback_client
        finally:
            fallback_client.close()


@pytest.fixture
def sample_item():
    """Sample item data for testing."""
    return {"name": "Test Item", "description": "A test item", "price": 9.99}
