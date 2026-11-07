"""Public Data API v2, with shared pacing and server-directed backoff."""
import asyncio
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

BASE_URL = "https://data-api.polymarket.com"


class ApiError(Exception):
    pass


def retry_seconds(value: str | None) -> float:
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 1.0


class API:
    def __init__(self, rps=10.0, transport=None):
        self.client = httpx.AsyncClient(
            base_url=BASE_URL, timeout=20, transport=transport,
            headers={"User-Agent": "polymarket-watch/0.1", "Cache-Control": "no-cache"},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        )
        self.spacing = 1 / rps
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.requests = 0
        self.retries = 0
        self.cache_age = 0.0

    async def close(self):
        await self.client.aclose()

    async def pace(self):
        async with self.lock:
            await asyncio.sleep(max(0, self.next_request - time.monotonic(), self.blocked_until - time.monotonic()))
            self.next_request = time.monotonic() + self.spacing

    async def get(self, path, *, envelope=True, cache_bust=True, **params):
        for attempt in range(5):
            await self.pace()
            # The live CDN otherwise returns cached feed heads (up to 300s).
            # This accepted, unknown query key gives every request a fresh URL.
            query = {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items() if v is not None}
            if cache_bust:
                query["_"] = str(time.time_ns())
            try:
                self.requests += 1
                response = await self.client.get(path, params=query)
                self.cache_age = float(response.headers.get("age", "0"))
                if response.status_code == 429 or response.status_code >= 500:
                    delay = max(retry_seconds(response.headers.get("retry-after")), min(30, 2 ** attempt))
                    self.blocked_until = max(self.blocked_until, time.monotonic() + delay + random.uniform(0, .25))
                    self.retries += 1
                    continue
                response.raise_for_status()
                data = response.json()
                if envelope and (not isinstance(data, dict) or "data" not in data):
                    raise ApiError(f"Unexpected API response from {path}")
                return data
            except (httpx.TransportError, ValueError) as exc:
                if attempt == 4:
                    raise ApiError(f"{path}: {exc}") from exc
                self.retries += 1
                await asyncio.sleep(min(15, 2 ** attempt))
            except httpx.HTTPStatusError as exc:
                try:
                    body = exc.response.json()
                    detail = str(body.get("error", ""))[:240] if isinstance(body, dict) else ""
                except ValueError:
                    detail = ""
                raise ApiError(f"{path}: HTTP {exc.response.status_code} {detail}; trace {exc.response.headers.get('x-trace-id', '?')}") from exc
        raise ApiError(f"{path}: unavailable after backoff")

    async def pages(self, path, max_pages=20, **params):
        rows = []
        for _ in range(max_pages):
            page = await self.get(path, **params)
            if not isinstance(page["data"], list):
                raise ApiError(f"{path}: expected a list")
            rows.extend(page["data"])
            cursor = page.get("pagination", {}).get("next_cursor")
            if not cursor:
                return rows, True
            params["cursor"] = cursor
        return rows, False
