"""Async Python client for mijn.ista.nl."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://mijn.ista.nl"
_HOME_LOGIN = "/home/login"
_HOME_PAGE = "/Home"
_USER_VALUES = "/api/Values/UserValues"
_MONTH_VALUES = "/api/Consumption/MonthValues"
_CONSUMPTION_VALUES = "/api/Values/ConsumptionValues"
_CONSUMPTION_AVERAGES = "/api/Values/ConsumptionAverages"

_TIMEOUT = aiohttp.ClientTimeout(total=30)

# mijn.ista.nl authenticates through an OpenID Connect (Keycloak) login at
# login.ista.com, not through a bearer token issued by the app itself. Every
# data endpoint still expects an `Authorization: Bearer` header to be present,
# but the value is never actually validated by the server — it's the same
# fixed placeholder the site's own frontend hardcodes ("i want a tasty
# cookie", base64-encoded). Real authorization comes from the ASP.NET Core
# session cookie plus the CSRF header below; this header just has to exist.
_PLACEHOLDER_BEARER = "Bearer aSB3YW50IGEgdGFzdHkgY29va2ll"
_CSRF_HEADER = "x-csrf-token-ista-nl_tp"

# MonthValues streams data in shards; poll until hs >= sh or time budget expires.
# The server loads ~0.3 shards/s and may have 20+ shards (months of history).
# We use a time budget so callers get a predictable upper bound regardless of
# how many shards the server has. The most recent month loads first, so even
# a partial result is enough for all "Month" sensors.
_MONTH_SHARD_BUDGET_S = 60       # full mode: wait for complete history
_MONTH_SHARD_BUDGET_QUICK_S = 15  # quick mode: just enough for first useful shard
_MONTH_SHARD_DELAY = 2  # seconds between shard polls

# Transient server errors that warrant a retry with backoff (non-shard paths only).
_RETRY_STATUSES = {503}
_MAX_RETRIES = 3  # retries after the first attempt (4 total)


class MijnIstaAuthError(Exception):
    """Raised when credentials are rejected by mijn.ista.nl."""


class MijnIstaConnectionError(Exception):
    """Raised when the API cannot be reached."""


def _extract(pattern: str, text: str, flags: int = 0) -> str | None:
    m = re.search(pattern, text, flags)
    return html.unescape(m.group(1)) if m else None


class MijnIstaAPI:
    """Async HTTP client for mijn.ista.nl.

    Authentication is an OpenID Connect Authorization Code + PKCE flow
    against ista's Keycloak instance (login.ista.com). The PKCE code
    verifier and the OIDC state/nonce are generated and tracked entirely
    server-side by mijn.ista.nl (tied to short-lived cookies set during the
    challenge) — this client only needs to carry cookies through the
    redirect chain and submit the Keycloak login form, exactly like a
    browser without JavaScript would. The result is an ASP.NET Core
    identity session cookie (held in the shared aiohttp session's cookie
    jar) plus a CSRF token that must accompany every data request.
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
        self._csrf_token: str | None = None

    # ── authentication ──────────────────────────────────────────────────────

    async def authenticate(self) -> None:
        """Log in via the OIDC flow and establish the session cookie + CSRF token.

        Raises MijnIstaAuthError on bad credentials,
        MijnIstaConnectionError on network failure or an unexpected response.
        """
        try:
            # Start every login from a clean slate. The session is shared and
            # long-lived (Home Assistant hands the same aiohttp session to all
            # integrations), so cookies from an earlier login survive into the
            # next call. With an ista session cookie still in the jar the
            # priming request below no longer serves the landing page — it
            # redirects into the portal's sign-out flow, whose
            # /signout-callback-oidc leg answers 403 and leaves the session
            # wedged, so /home/login answers 403 as well. Only ista's own
            # domains are dropped, never another integration's cookies.
            self._session.cookie_jar.clear_domain("ista.nl")
            self._session.cookie_jar.clear_domain("ista.com")

            # Priming request: establishes the load-balancer affinity cookie
            # that /home/login's challenge redirect depends on.
            async with self._session.get(f"{BASE_URL}/", timeout=_TIMEOUT):
                pass

            async with self._session.get(
                f"{BASE_URL}{_HOME_LOGIN}",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": f"{BASE_URL}/",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Site": "same-origin",
                },
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                authorize_url = resp.headers.get("Location")
                if resp.status != 302 or not authorize_url:
                    raise MijnIstaConnectionError(
                        f"Unexpected response starting login (HTTP {resp.status})"
                    )

            async with self._session.get(authorize_url, timeout=_TIMEOUT) as resp:
                login_html = await resp.text()

            form_action = _extract(
                r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', login_html
            )
            if not form_action:
                raise MijnIstaConnectionError(
                    "Could not find the identity provider's login form"
                )

            async with self._session.post(
                form_action,
                data={
                    "username": self._username,
                    "password": self._password,
                    "credentialId": "",
                },
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                body = await resp.text()

            if 'id="kc-form-login"' in body:
                raise MijnIstaAuthError("Invalid credentials")

            callback_action = _extract(r'ACTION="([^"]+)"', body)
            callback_fields = {
                k: html.unescape(v)
                for k, v in re.findall(r'NAME="([^"]+)" VALUE="([^"]*)"', body)
            }
            if not callback_action or "code" not in callback_fields:
                raise MijnIstaConnectionError(
                    "Unexpected response from the identity provider"
                )

            async with self._session.post(
                callback_action,
                data=callback_fields,
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                if resp.status not in (302, 303):
                    raise MijnIstaConnectionError(
                        f"Session callback failed (HTTP {resp.status})"
                    )

            await self._refresh_csrf_token()

        except MijnIstaAuthError:
            raise
        except MijnIstaConnectionError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise MijnIstaConnectionError(str(exc)) from exc

        _LOGGER.debug("mijn.ista.nl: authenticated successfully")

    async def _refresh_csrf_token(self) -> None:
        """Fetch the CSRF token every data POST must carry, from the dashboard page."""
        async with self._session.get(f"{BASE_URL}{_HOME_PAGE}", timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                raise MijnIstaAuthError("Session was not accepted after login")
            page_html = await resp.text()

        token = _extract(r'<meta name="csrf-token" content="([^"]+)"', page_html)
        if not token:
            raise MijnIstaConnectionError("Could not find CSRF token on dashboard page")
        self._csrf_token = token

    # ── data endpoints ──────────────────────────────────────────────────────

    async def get_user_values(self) -> dict[str, Any]:
        """POST /api/Values/UserValues.

        Returns the address list, available services, billing periods,
        and the current-vs-previous annual comparison.
        """
        return await self._post(_USER_VALUES, {})

    async def get_month_values(
        self, cuid: str, *, quick: bool = False
    ) -> dict[str, Any]:
        """POST /api/Consumption/MonthValues, polling until shards are loaded.

        The API streams data in shards (hs = loaded, sh = total) and may return
        425 on the very first request when data is not yet ready. All attempts
        use _poll_shard (no retry) — the loop itself handles the wait-and-retry.

        When ``quick=True`` the call returns as soon as the first response with
        any mc entries arrives (≤ 15 s). This is used during HA startup so the
        integration does not block Home Assistant for a full minute. Subsequent
        coordinator refreshes use ``quick=False`` (default) for complete history.
        """
        budget = _MONTH_SHARD_BUDGET_QUICK_S if quick else _MONTH_SHARD_BUDGET_S
        data: dict[str, Any] = {}
        deadline = time.monotonic() + budget
        attempt = 0

        while time.monotonic() < deadline:
            polled = await self._poll_shard(_MONTH_VALUES, {"Cuid": cuid})
            if polled is not None:
                data = polled
                sh = data.get("sh", 0)
                hs = data.get("hs", 0)
                if sh > 0 and hs >= sh:
                    _LOGGER.debug(
                        "mijn.ista.nl: MonthValues complete (%d shards)", sh
                    )
                    break
                if quick and data.get("mc"):
                    _LOGGER.debug(
                        "mijn.ista.nl: MonthValues quick mode done "
                        "(%d mc entries, shard %d/%d)",
                        len(data["mc"]), hs, sh,
                    )
                    break
                _LOGGER.debug(
                    "mijn.ista.nl: MonthValues shard %d/%d, waiting %ds",
                    hs, sh, _MONTH_SHARD_DELAY,
                )
            else:
                _LOGGER.debug(
                    "mijn.ista.nl: MonthValues not ready (attempt %d), waiting %ds",
                    attempt + 1, _MONTH_SHARD_DELAY,
                )
            attempt += 1
            await asyncio.sleep(_MONTH_SHARD_DELAY)

        if not quick and data.get("sh", 0) > data.get("hs", 0):
            _LOGGER.warning(
                "mijn.ista.nl: MonthValues time budget exceeded, "
                "returning %d/%d shards for %s",
                data.get("hs", 0), data.get("sh", 0), cuid,
            )

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
        """Build a request body with LANG merged with caller extras."""
        return {"LANG": self._lang, **extra}

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Authorization": _PLACEHOLDER_BEARER}
        if self._csrf_token:
            headers[_CSRF_HEADER] = self._csrf_token
        return headers

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
                headers=self._auth_headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    await self.authenticate()
                    return None  # caller will retry on next shard poll
                if resp.status not in {200, 425}:
                    _LOGGER.debug(
                        "mijn.ista.nl: shard poll returned HTTP %d, will retry",
                        resp.status,
                    )
                    return None
                # Read body even on 425 — the server includes shard metadata
                # (hs/sh fields) so the loop can track progress.
                try:
                    data: dict[str, Any] = await resp.json()
                except Exception:
                    return None
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
                    url,
                    json=self._body(extra),
                    headers=self._auth_headers(),
                    timeout=_TIMEOUT,
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
                        _LOGGER.debug("mijn.ista.nl: session expired, re-authenticating")
                        await self.authenticate()
                        async with self._session.post(
                            url,
                            json=self._body(extra),
                            headers=self._auth_headers(),
                            timeout=_TIMEOUT,
                        ) as retry:
                            retry.raise_for_status()
                            return await retry.json()
                    resp.raise_for_status()
                    return await resp.json()
        except MijnIstaAuthError:
            raise
        except MijnIstaConnectionError:
            raise
        except aiohttp.ClientResponseError as exc:
            raise MijnIstaConnectionError(str(exc)) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise MijnIstaConnectionError(str(exc)) from exc
        raise MijnIstaConnectionError(f"Service unavailable after retries: {path}")
