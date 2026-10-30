"""Admin "Genel Bakış": exchange rates, members, B2B applications and sales KPIs."""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.b2b import ApplicationStatus, B2BApplication
from app.models.order import PAID_STATUSES, Order, PaymentMethod
from app.models.user import User, UserRole
from app.schemas.b2b import DashboardRead, PeriodTotals
from app.services.exchange_rates import RatesUnavailable, get_rates

router = APIRouter(prefix="/admin", tags=["admin-dashboard"])

TR = ZoneInfo("Europe/Istanbul")
PAID = PAID_STATUSES
ZERO = Decimal("0.00")


def period_starts(now: datetime) -> dict[str, datetime]:
    """Start of today (00:00), this week (Monday), month and year, in Turkey time."""
    local = now.astimezone(TR)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "daily": midnight,
        "weekly": midnight - timedelta(days=local.weekday()),
        "monthly": midnight.replace(day=1),
        "yearly": midnight.replace(month=1, day=1),
    }


async def _totals(db: AsyncSession, since: datetime) -> PeriodTotals:
    """Paid orders since `since` (by payment date); amounts are what the customer paid."""
    paid = Order.total_amount + Order.shipping_cost - Order.discount_amount
    row = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(paid), 0),
                func.coalesce(func.sum(case((Order.payment_method == PaymentMethod.CREDIT_CARD, paid), else_=0)), 0),
                func.coalesce(func.sum(case((Order.payment_method == PaymentMethod.BANK_TRANSFER, paid), else_=0)), 0),
            ).where(Order.status.in_(PAID), Order.paid_at.is_not(None), Order.paid_at >= since)
        )
    ).one()
    count, total, card, transfer = row
    q = lambda v: Decimal(v).quantize(ZERO)  # noqa: E731
    return PeriodTotals(order_count=count, order_total=q(total), card_total=q(card), transfer_total=q(transfer))


@router.get("/dashboard", response_model=DashboardRead)
async def dashboard(_admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    try:
        rates = await get_rates()
        usd, eur, rates_date = rates.try_per_unit["USD"], rates.try_per_unit["EUR"], rates.bulletin_date.isoformat()
    except RatesUnavailable:
        usd = eur = rates_date = None

    starts = period_starts(datetime.now(TR))
    return DashboardRead(
        usd_try=usd,
        eur_try=eur,
        rates_date=rates_date,
        member_count=await db.scalar(select(func.count()).select_from(User).where(User.role == UserRole.CUSTOMER)),
        pending_applications=await db.scalar(
            select(func.count()).select_from(B2BApplication).where(B2BApplication.status == ApplicationStatus.PENDING)
        ),
        daily=await _totals(db, starts["daily"]),
        weekly=await _totals(db, starts["weekly"]),
        monthly=await _totals(db, starts["monthly"]),
        yearly=await _totals(db, starts["yearly"]),
    )
