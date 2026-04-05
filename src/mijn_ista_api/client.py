"""Async Python client for mijn.ista.nl."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://mijn.ista.nl"
_AUTHORIZE = "/api/Authorization/Authorize"
_JWT_REFRESH = "/api/Authorization/JWTRefresh"
_USER_VALUES = "/api/Values/UserValues"
_MONTH_VALUES = "/api/Consumption/MonthValues"
_CONSUMPTION_VALUES = "/api/Values/ConsumptionValues"
_CONSUMPTION_AVERAGES = "/api/Values/ConsumptionAverages"

_TIMEOUT = aiohttp.ClientTimeout(total=30)

# MonthValues streams data in shards; poll until hs >= sh.
# Shard polls use a no-retry path — 425 means "not ready yet" (handled by the
# sleep+loop), so we must NOT run the full backoff retry on each poll.
_MONTH_SHARD_MAX_POLLS = 8
_MONTH_SHARD_DELAY = 2  # seconds between shard polls

# Transient server errors that warrant a retry with backoff (non-shard paths only).
_RETRY_STATUSES = {503}
_MAX_RETRIES = 3  # retries after the first attempt (4 total)


class MijnIstaAuthError(Exception):
    """Raised when credentials are rejected by mijn.ista.nl."""


class MijnIstaConnectionError(Exception):
    """Raised when the API cannot be reached."""


class MijnIstaAPI:
    """Async HTTP client for mijn.ista.nl.

    Authentication uses a custom JWT that is passed in the request body
    (not as an Authorization header). Every response carries a refreshed
    JWT that must replace the stored one for the next call.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        lang: str = "nl-NL",
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._lang = lang
        self._jwt: str | None = None

    # ── authentication ──────────────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Obtain a fresh JWT from /api/Authorization/Authorize.

        Raises MijnIstaAuthError on bad credentials,
        MijnIstaConnectionError on network failure.
        """
        try:
            async with self._session.post(
                f"{BASE_URL}{_AUTHORIZE}",
                json={
                    "username": self._username,
                    "password": self._password,
                    "LANG": self._lang,
                },
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 400:
                    raise MijnIstaAuthError("Invalid credentials (HTTP 400)")
                resp.raise_for_status()
                data: dict[str, Any] = await resp.json()
        except MijnIstaAuthError:
            raise
        except aiohttp.ClientResponseError as exc:
            if exc.status == 400:
                raise MijnIstaAuthError("Invalid credentials") from exc
            raise MijnIstaConnectionError(str(exc)) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise MijnIstaConnectionError(str(exc)) from exc

        if "JWT" not in data:
            raise MijnIstaAuthError("No JWT field in /Authorize response")
        self._jwt = data["JWT"]
        _LOGGER.debug("mijn.ista.nl: authenticated successfully")

    # ── data endpoints ──────────────────────────────────────────────────────

    async def get_user_values(self) -> dict[str, Any]:
        """POST /api/Values/UserValues.

        Returns the address list, available services, billing periods,
        and the current-vs-previous annual comparison.
        """
        return await self._post(_USER_VALUES, {})

    async def get_month_values(self, cuid: str) -> dict[str, Any]:
        """POST /api/Consumption/MonthValues, polling until all shards are loaded.

        The API streams data in shards (hs = loaded, sh = total) and may return
        425 on the very first request when data is not yet ready. All attempts
        use _poll_shard (no retry) — the loop itself handles the wait-and-retry
        cycle. We poll every _MONTH_SHARD_DELAY seconds, up to
        _MONTH_SHARD_MAX_POLLS times total.
        """
        data: dict[str, Any] = {}

        for attempt in range(_MONTH_SHARD_MAX_POLLS):
            polled = await self._poll_shard(_MONTH_VALUES, {"Cuid": cuid})
            if polled is not None:
                data = polled
                sh = data.get("sh", 0)
                hs = data.get("hs", 0)
                if sh > 0 and hs >= sh:
                    break
                _LOGGER.debug(
                    "mijn.ista.nl: MonthValues loading shard %d/%d, waiting %ds",
                    hs, sh, _MONTH_SHARD_DELAY,
                )
            else:
                _LOGGER.debug(
                    "mijn.ista.nl: MonthValues not ready (attempt %d), waiting %ds",
                    attempt + 1, _MONTH_SHARD_DELAY,
                )
            if attempt < _MONTH_SHARD_MAX_POLLS - 1:
                await asyncio.sleep(_MONTH_SHARD_DELAY)

        return data

    async def get_consumption_values(
        self, cuid: str, billing_period: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /api/Values/ConsumptionValues.

        Returns meter totals for a specific billing period.
        billing_period shape: {"y": 2025, "s": "2025-01-01T00:00:00",
                                "e": "2025-12-31T00:00:00", "ta": 11}
        """
        return await self._post(
            _CONSUMPTION_VALUES, {"Cuid": cuid, "Billingperiod": billing_period}
        )

    async def get_consumption_averages(
        self, cuid: str, start: str, end: str
    ) -> dict[str, Any]:
        """POST /api/Values/ConsumptionAverages.

        Returns normalised building-wide averages per service.
        start/end format: "YYYY-MM-DD"
        """
        return await self._post(
            _CONSUMPTION_AVERAGES,
            {"Cuid": cuid, "PAR": {"start": start, "end": end, "cuid": cuid}},
        )

    # ── internals ───────────────────────────────────────────────────────────

    def _body(self, extra: dict[str, Any]) -> dict[str, Any]:
        """Build a request body with JWT + LANG merged with caller extras."""
        return {"JWT": self._jwt, "LANG": self._lang, **extra}

    def _absorb_jwt(self, data: dict[str, Any]) -> None:
        """Persist the refreshed JWT that every API response carries."""
        if refreshed := data.get("JWT"):
            self._jwt = refreshed

    async def _refresh_jwt(self) -> None:
        """Refresh JWT via /api/Authorization/JWTRefresh.

        Falls back to a full re-authentication if the refresh endpoint fails.
        """
        try:
            async with self._session.post(
                f"{BASE_URL}{_JWT_REFRESH}",
                json={"JWT": self._jwt, "LANG": self._lang},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if new_jwt := data.get("JWT"):
                        self._jwt = new_jwt
                        _LOGGER.debug("mijn.ista.nl: JWT refreshed via JWTRefresh")
                        return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        _LOGGER.debug("mijn.ista.nl: JWTRefresh failed, falling back to full re-auth")
        await self.authenticate()

    async def _poll_shard(
        self, path: str, extra: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Single POST with no retry logic, used exclusively for shard polling.

        Returns the parsed response dict, or None if the server is not ready
        (4xx other than 401) or a network error occurs. The caller's loop
        handles the wait-and-retry cycle.
        """
        try:
            async with self._session.post(
                f"{BASE_URL}{path}",
                json=self._body(extra),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    await self._refresh_jwt()
                    return None  # caller will retry on next shard poll
                if not resp.ok:
                    _LOGGER.debug(
                        "mijn.ista.nl: shard poll returned HTTP %d, will retry",
                        resp.status,
                    )
                    return None
                data: dict[str, Any] = await resp.json()
                self._absorb_jwt(data)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.debug("mijn.ista.nl: shard poll network error: %s", exc)
            return None

    async def _post(self, path: str, extra: dict[str, Any]) -> dict[str, Any]:
        """POST with exponential-backoff retry on 503 and re-auth on 401."""
        url = f"{BASE_URL}{path}"
        try:
            for attempt in range(_MAX_RETRIES + 1):
                async with self._session.post(
                    url, json=self._body(extra), timeout=_TIMEOUT
                ) as resp:
                    if resp.status in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                        wait = 3 * (attempt + 1)
                        _LOGGER.debug(
                            "mijn.ista.nl: HTTP %d on %s, retry %d in %ds",
                            resp.status, path, attempt + 1, wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 401:
                        _LOGGER.debug("mijn.ista.nl: JWT expired, refreshing")
                        await self._refresh_jwt()
                        async with self._session.post(
                            url, json=self._body(extra), timeout=_TIMEOUT
                        ) as retry:
                            retry.raise_for_status()
                            data: dict[str, Any] = await retry.json()
                            self._absorb_jwt(data)
                            return data
                    resp.raise_for_status()
                    data = await resp.json()
                    self._absorb_jwt(data)
                    return data
        except MijnIstaAuthError:
            raise
        except MijnIstaConnectionError:
            raise
        except aiohttp.ClientResponseError as exc:
            raise MijnIstaConnectionError(str(exc)) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise MijnIstaConnectionError(str(exc)) from exc
        raise MijnIstaConnectionError(f"Service unavailable after retries: {path}")
