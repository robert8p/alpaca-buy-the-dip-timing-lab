from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from typing import Any

import httpx

from .config import settings

DATA_URL = "https://data.alpaca.markets"
TRADING_URL = "https://paper-api.alpaca.markets"


class AlpacaError(RuntimeError):
    pass


class AlpacaClient:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            headers=settings.alpaca_headers,
            timeout=settings.request_timeout_seconds,
        )
        self._lock = asyncio.Lock()
        self._request_times: list[float] = []

    async def close(self) -> None:
        await self.client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._request_times = [t for t in self._request_times if now - t < 60]
            if len(self._request_times) >= max(1, settings.alpaca_market_data_rpm - 2):
                wait = 60 - (now - self._request_times[0]) + 0.05
                await asyncio.sleep(max(0.05, wait))
                now = time.monotonic()
                self._request_times = [t for t in self._request_times if now - t < 60]
            self._request_times.append(time.monotonic())

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any] | list[Any]:
        last_error = "unknown"
        for attempt in range(settings.max_request_retries):
            await self._throttle()
            try:
                response = await self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                await asyncio.sleep(min(30, 2**attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:500]}"
                retry_after = float(response.headers.get("retry-after", "0") or 0)
                await asyncio.sleep(max(retry_after, min(30, 2**attempt)))
                continue
            if response.status_code >= 400:
                raise AlpacaError(f"HTTP {response.status_code}: {response.text[:1000]}")
            return response.json()
        raise AlpacaError(f"Alpaca request failed after retries: {last_error}")

    async def calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        data = await self._get(
            f"{TRADING_URL}/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
        )
        if not isinstance(data, list):
            raise AlpacaError("Unexpected calendar response")
        return data

    async def bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        adjustment: str = "split",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeframe": "1Min",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adjustment": adjustment,
            "feed": settings.alpaca_feed,
            "limit": 10000,
            "sort": "asc",
        }
        bars: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            if page_token:
                params["page_token"] = page_token
            payload = await self._get(f"{DATA_URL}/v2/stocks/{symbol}/bars", params)
            if not isinstance(payload, dict):
                raise AlpacaError("Unexpected bars response")
            bars.extend(payload.get("bars") or [])
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return bars
