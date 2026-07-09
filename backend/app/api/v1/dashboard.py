from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
from decimal import Decimal, ROUND_HALF_UP
from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request as get_current_user

router = APIRouter()

# Currency is tracked to sub-cent precision (NUMERIC(10,3)); expose it rounded to
# the cent for the dashboard.
CENTS = Decimal("0.01")

@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:

    tenant_id = getattr(current_user, "tenant_id", "default_tenant") or "default_tenant"

    revenue_data = await get_revenue_summary(property_id, tenant_id)

    # Do NOT round-trip money through binary float: float() can't represent most
    # decimal cent values exactly, so totals drift by fractions of a cent (the
    # "few cents off" finance reports, and the UI's "precision mismatch" flag).
    # Keep it Decimal and round to the cent exactly once, here at the boundary.
    total_revenue = Decimal(str(revenue_data['total'])).quantize(CENTS, rounding=ROUND_HALF_UP)

    return {
        "property_id": revenue_data['property_id'],
        "total_revenue": float(total_revenue),
        "currency": revenue_data['currency'],
        "reservations_count": revenue_data['count']
    }
