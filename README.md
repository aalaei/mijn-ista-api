# mijn-ista-api

Async Python client for the [mijn.ista.nl](https://mijn.ista.nl) energy monitoring portal.

## Installation

```bash
pip install mijn-ista-api
```

## Requirements

- Python 3.12+
- aiohttp 3.9+

## Usage

```python
import asyncio
import aiohttp
from mijn_ista_api import MijnIstaAPI, MijnIstaAuthError, MijnIstaConnectionError

async def main():
    async with aiohttp.ClientSession() as session:
        api = MijnIstaAPI(session, "you@example.com", "your-password", lang="nl-NL")

        # Log in (OpenID Connect flow against login.ista.com)
        await api.authenticate()

        # Fetch account + annual comparison data
        user_data = await api.get_user_values()
        for cus in user_data.get("Cus", []):
            cuid = cus["Cuid"]
            print(cus.get("Adress"), cus.get("City"))

            # Full monthly history (polls until all shards are loaded)
            month_data = await api.get_month_values(cuid)

            # Building averages for the current billing year
            periods = cus.get("curConsumption", {}).get("BillingPeriods", [])
            if periods:
                p = sorted(periods, key=lambda x: x["y"], reverse=True)[0]
                avg_data = await api.get_consumption_averages(
                    cuid, p["s"][:10], p["e"][:10]
                )

asyncio.run(main())
```

## API overview

| Method | Endpoint | Description |
|---|---|---|
| `authenticate()` | OIDC login via `login.ista.com` | Establish an authenticated session |
| `get_user_values()` | `POST /api/Values/UserValues` | Account info, annual comparison |
| `get_month_values(cuid)` | `POST /api/Consumption/MonthValues` | Full monthly history (auto-polls shards) |
| `get_consumption_values(cuid, billing_period)` | `POST /api/Values/ConsumptionValues` | Meter totals for one billing year |
| `get_consumption_averages(cuid, start, end)` | `POST /api/Values/ConsumptionAverages` | Building-wide normalised averages |

## Error handling

```python
from mijn_ista_api import MijnIstaAuthError, MijnIstaConnectionError

try:
    await api.authenticate()
except MijnIstaAuthError:
    # Bad credentials
    ...
except MijnIstaConnectionError:
    # Network error or API unavailable
    ...
```

## Notes

- `authenticate()` drives a full OpenID Connect Authorization Code + PKCE login against ista's
  Keycloak instance (`login.ista.com`), the same flow the website itself uses. It establishes an
  ASP.NET Core session cookie (held by the `aiohttp.ClientSession` you provide) plus a CSRF token
  that every data call must include; there is no bearer token to manage yourself.
- Accounts with two-factor authentication enabled are not supported — the login form fill-in
  assumes a single username/password step.
- A 401 from any data endpoint triggers a full re-login (`authenticate()`) followed by one retry.
- `get_month_values` polls until the server has loaded all data shards (the API streams results).
- Transient `503 Service Unavailable` responses are retried with exponential backoff.

## Home Assistant integration

This library powers the [ista Nederland](https://github.com/aalaei/ha-mijn-ista) Home Assistant custom integration.

## License

MIT
