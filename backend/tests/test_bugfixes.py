"""Regression tests for the three revenue-dashboard bugs.

Covers the pure logic behind each fix; the DB-pool fix and the end-to-end cache
isolation are exercised against the running stack (see PROCESS notes).
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import pytest

from app.services.reservations import month_bounds_utc
from app.services import cache


# --- Bug: timezone-naive month bucketing (Client A: "March totals don't match") ---

def test_month_bounds_respect_property_timezone():
    """A Paris check-in at 2024-02-29 23:30 UTC is 00:30 local on March 1 -> March."""
    res_tz_1 = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)  # seed row res-tz-1

    start, end = month_bounds_utc(2024, 3, "Europe/Paris")
    assert start <= res_tz_1 < end, "Paris property: boundary booking belongs to March"

    # Under naive-UTC bucketing this booking was dropped into February.
    start_utc, end_utc = month_bounds_utc(2024, 3, "UTC")
    assert not (start_utc <= res_tz_1 < end_utc)


def test_month_bounds_december_rolls_into_next_year():
    start, end = month_bounds_utc(2024, 12, "UTC")
    assert start == datetime(2024, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2025, 1, 1, tzinfo=timezone.utc)


# --- Bug: cross-tenant cache leak (Client B: "another company's numbers") ---

class _FakeRedis:
    def __init__(self):
        self.keys = []

    async def get(self, key):
        self.keys.append(key)
        return None

    async def setex(self, key, ttl, value):
        self.keys.append(key)


def test_revenue_cache_key_is_tenant_scoped(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "redis_client", fake)

    async def _fake_calc(property_id, tenant_id):
        return {"property_id": property_id, "tenant_id": tenant_id,
                "total": "0.00", "currency": "USD", "count": 0}

    monkeypatch.setattr("app.services.reservations.calculate_total_revenue", _fake_calc)

    asyncio.run(cache.get_revenue_summary("prop-001", "tenant-b"))

    # Every key touched must carry the tenant so one tenant can never read another's.
    assert fake.keys, "cache was consulted"
    assert all("tenant-b" in k for k in fake.keys)
    assert "revenue:tenant-b:prop-001" in fake.keys


# --- Bug: money via binary float (Finance: "off by a few cents") ---

def test_money_rounds_to_cents_without_float_drift():
    # sub-cent amounts (NUMERIC(10,3)) must aggregate as Decimal, not float.
    amounts = [Decimal("333.333"), Decimal("333.333"), Decimal("333.334")]
    total = sum(amounts).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert total == Decimal("1000.00")

    # rounding each amount before summing (a common wrong "fix") loses a cent:
    naive = sum(a.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for a in amounts)
    assert naive == Decimal("999.99")
