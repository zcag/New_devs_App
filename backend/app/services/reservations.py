from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, List, Tuple
from zoneinfo import ZoneInfo


def month_bounds_utc(year: int, month: int, tz_name: str) -> Tuple[datetime, datetime]:
    """UTC ``[start, end)`` instants bounding ``month`` in the property's LOCAL timezone.

    Revenue is reported per local calendar month, so the month boundaries must be
    anchored in the property's timezone and then converted to UTC for comparison
    against ``check_in_date`` (a timestamptz stored in UTC).

    The previous implementation built *naive* boundaries (``datetime(year, month, 1)``)
    and compared them against tz-aware timestamps — effectively bucketing by UTC and
    ignoring each property's timezone. That mis-files bookings near a month edge:
    e.g. a Europe/Paris check-in at ``2024-02-29 23:30 UTC`` is ``2024-03-01 00:30``
    local, i.e. **March** revenue, but naive-UTC bucketing drops it into February —
    exactly the "March totals don't match" report.
    """
    tz = ZoneInfo(tz_name or "UTC")
    start_local = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def calculate_monthly_revenue(property_id: str, tenant_id: str, month: int, year: int) -> Decimal:
    """Revenue for ``property_id`` in the given local-calendar month (tenant-scoped)."""
    from app.core.database_pool import db_pool
    from sqlalchemy import text

    await db_pool.initialize()
    async with db_pool.get_session() as session:
        # The property's own timezone determines what "this month" means.
        tz_row = (await session.execute(
            text("SELECT timezone FROM properties WHERE id = :property_id AND tenant_id = :tenant_id"),
            {"property_id": property_id, "tenant_id": tenant_id},
        )).fetchone()
        tz_name = tz_row.timezone if tz_row else "UTC"

        start_utc, end_utc = month_bounds_utc(year, month, tz_name)

        row = (await session.execute(
            text(
                """
                SELECT COALESCE(SUM(total_amount), 0) AS total
                FROM reservations
                WHERE property_id = :property_id
                  AND tenant_id = :tenant_id
                  AND check_in_date >= :start_utc
                  AND check_in_date < :end_utc
                """
            ),
            {"property_id": property_id, "tenant_id": tenant_id, "start_utc": start_utc, "end_utc": end_utc},
        )).fetchone()

        return Decimal(str(row.total))

async def calculate_total_revenue(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Aggregates revenue from database.
    """
    try:
        # Reuse the shared, lazily-initialized pool (initialize() is idempotent);
        # constructing a new DatabasePool per request would leak an engine each call.
        from app.core.database_pool import db_pool
        await db_pool.initialize()
        
        if db_pool.session_factory:
            async with db_pool.get_session() as session:
                # Use SQLAlchemy text for raw SQL
                from sqlalchemy import text
                
                query = text("""
                    SELECT 
                        property_id,
                        SUM(total_amount) as total_revenue,
                        COUNT(*) as reservation_count
                    FROM reservations 
                    WHERE property_id = :property_id AND tenant_id = :tenant_id
                    GROUP BY property_id
                """)
                
                result = await session.execute(query, {
                    "property_id": property_id, 
                    "tenant_id": tenant_id
                })
                row = result.fetchone()
                
                if row:
                    total_revenue = Decimal(str(row.total_revenue))
                    return {
                        "property_id": property_id,
                        "tenant_id": tenant_id,
                        "total": str(total_revenue),
                        "currency": "USD", 
                        "count": row.reservation_count
                    }
                else:
                    # No reservations found for this property
                    return {
                        "property_id": property_id,
                        "tenant_id": tenant_id,
                        "total": "0.00",
                        "currency": "USD",
                        "count": 0
                    }
        else:
            raise Exception("Database pool not available")
            
    except Exception as e:
        print(f"Database error for {property_id} (tenant: {tenant_id}): {e}")
        
        # Create property-specific mock data for testing when DB is unavailable
        # This ensures each property shows different figures
        mock_data = {
            'prop-001': {'total': '1000.00', 'count': 3},
            'prop-002': {'total': '4975.50', 'count': 4}, 
            'prop-003': {'total': '6100.50', 'count': 2},
            'prop-004': {'total': '1776.50', 'count': 4},
            'prop-005': {'total': '3256.00', 'count': 3}
        }
        
        mock_property_data = mock_data.get(property_id, {'total': '0.00', 'count': 0})
        
        return {
            "property_id": property_id,
            "tenant_id": tenant_id, 
            "total": mock_property_data['total'],
            "currency": "USD",
            "count": mock_property_data['count']
        }
