# Findings — Property Revenue Dashboard

Four defects in the revenue path explain all three client reports. Each fix is
small and in-place (no rewrites), and is covered by a regression test in
`backend/tests/test_bugfixes.py`.

## How to reproduce
```bash
docker-compose up --build
# log in as each client and hit GET /api/v1/dashboard/summary?property_id=prop-001
#   Sunset (tenant-a):  sunset@propertyflow.com / client_a_2024   -> real total 2250.00 / 4 bookings
#   Ocean  (tenant-b):  ocean@propertyflow.com  / client_b_2024   -> real total 0.00 / 0 bookings
```
Seed truth: `tenant-a/prop-001` = **2250.000 (4)**, `tenant-b/prop-001` = **0**. Note `prop-001`
exists under *both* tenants (PK is `(id, tenant_id)`) — Sunset's "Beach House Alpha" vs Ocean's "Mountain Lodge Beta".

---

## Bug 1 — Dashboard silently served hard-coded mock data  → *Client A: "totals don't match"*
**Root cause.** `core/database_pool.py` built its DSN from `settings.supabase_db_user/host/...`,
which don't exist on `Settings` (`extra="ignore"` means they can never exist). Every request
raised `AttributeError`, the pool never initialized, and `services/reservations.calculate_total_revenue`
swallowed the error and returned a tenant-agnostic `mock_data` dict. So Sunset saw **$1000/3** instead
of their real **$2250/4**. (A second latent defect: `get_session` was `async def`, so
`async with db_pool.get_session()` received a coroutine.)
**Fix.** Build the engine from the configured `settings.database_url` (async driver), drop the
sync `QueuePool` (invalid on an async engine), and make `get_session` return the session directly.
Real, tenant-scoped SQL now runs. Also made `initialize()` idempotent and switched
`calculate_total_revenue` to the shared pool — it previously built a fresh engine per request
(connection leak). `database_pool.py`, `reservations.py`.

## Bug 2 — Cross-tenant revenue leak via the cache  → *Client B: "another company's numbers on refresh"*
**Root cause.** `services/cache.py` keyed the Redis entry on `revenue:{property_id}` only. Because
`prop-001` exists for both tenants, whichever tenant populated the 5-minute cache first served its
revenue to the other. Reproduced: with the cache cold, Sunset loads `prop-001` ($2250); Ocean then
loads `prop-001` and gets **$2250** — Sunset's data. The "sometimes" is cache-population order.
**Fix.** Scope the key by tenant: `revenue:{tenant_id}:{property_id}`. Ocean now correctly gets $0.
`cache.py`.

## Bug 3 — Money round-tripped through binary float  → *Finance: "off by a few cents"*
**Root cause.** `api/v1/dashboard.py` returned `float(revenue_data['total'])`. Amounts are stored to
sub-cent precision (`NUMERIC(10,3)`); converting Decimal money to binary float can't represent most
cent values exactly, so totals drift by fractions of a cent (the UI even ships a "Precision Mismatch"
guard for this).
**Fix.** Keep the value as `Decimal` and round to the cent exactly once at the response boundary
(`quantize(0.01, ROUND_HALF_UP)`) — never through float. `dashboard.py`.

## Bug 4 — Timezone-naive monthly bucketing  → *Client A: "properties in different time zones"*
**Root cause.** `services/reservations.calculate_monthly_revenue` built **naive** month boundaries
(`datetime(year, month, 1)`) and compared them to tz-aware `check_in_date`, ignoring each property's
`timezone`. Revenue is per **local** calendar month, so a Europe/Paris check-in at
`2024-02-29 23:30 UTC` (= `00:30` local on **March 1**, seed row `res-tz-1`, $1250) was dropped into
February — undercounting March.
**Fix.** Anchor month boundaries in the property's timezone, convert to UTC for the timestamptz
comparison, and complete the (previously stubbed) query, tenant-scoped. `reservations.py` +
`month_bounds_utc()` helper (unit-tested).

---

### Tests
`docker-compose exec backend python -m pytest tests/test_bugfixes.py` → 4 passing (timezone bounds
incl. the res-tz-1 boundary and Dec→year rollover; tenant-scoped cache key; cent-rounding without drift).
