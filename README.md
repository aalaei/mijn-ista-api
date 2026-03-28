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

        # Authenticate (obtains JWT)
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
| `authenticate()` | `POST /api/Authorization/Authorize` | Obtain JWT |
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

- The JWT is passed in the **request body**, not as an `Authorization` header.
- Every API response returns a refreshed JWT; the client handles this automatically.
- `get_month_values` polls until the server has loaded all data shards (the API streams results).
- Transient `425 Too Early` / `503 Service Unavailable` responses are retried with exponential backoff.

## Home Assistant integration

This library powers the [ista Nederland](https://github.com/aalaei/ha-mijn-ista-api) Home Assistant custom integration.

## License

MIT
