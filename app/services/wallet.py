"""Sultan Puan wallet: balance, expiry, and awards.

What a point is worth, how many an order earns and the annual bonus are store
parameters (``LoyaltyParameters``, edited under "Parametreler"); they are read
when an order is paid, not baked in here. Points expire one year after they are
earned.

The ledger keeps its original source names: ``PARAPUAN`` is a standard Sultan
Puan award and ``KUMBARA`` the annual bonus (which took over from Kumbarapuan),
so rows written before the rename read the same as rows written after it. The ledger is
append-only; the balance is derived by replaying it in order, with spending
consuming the soonest-expiring points first (FIFO by expiry). A row whose order was
cancelled is skipped, so points spent on it come back with their original expiry
and points earned on it (an approved order that was later cancelled) are void.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.money import format_tl
from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.order import PAID_STATUSES, VOID_STATUSES, Order
from app.models.user import User
from app.services.perks import retail_perks
from app.services.store_parameters import DEFAULT_LOYALTY, LoyaltyParameters, load_parameters

POINT_LIFETIME = timedelta(days=365)
EXPIRY_WARNING = timedelta(days=30)

def points_for(subtotal: Decimal, loyalty: LoyaltyParameters = DEFAULT_LOYALTY) -> int:
    """Standard Sultan Puan for a single order of this subtotal."""
    return loyalty.points_for(subtotal)


@dataclass
class Lot:
    entry_id: uuid.UUID
    source: str | None
    amount: int
    remaining: int
    created_at: datetime
    expires_at: datetime | None


@dataclass
class MilestoneProgress:
    """Where a customer stands on the annual bonus."""

    # Paid order totals counted so far: within the rolling window, and after
    # the last bonus if there was one inside it.
    spent: Decimal
    target: Decimal
    bonus_points: int
    period_days: int
    # Where the count starts: the window's start, or the last bonus if later.
    counting_since: datetime
    # True when the last bonus falls inside the window (the count restarted there).
    restarted: bool

    @property
    def remaining(self) -> Decimal:
        return max(self.target - self.spent, Decimal("0.00"))


@dataclass
class Wallet:
    balance: int
    lots: list[Lot] = field(default_factory=list)  # live lots with points left, soonest expiry first
    expiring_soon: int = 0  # points expiring within EXPIRY_WARNING


async def get_wallet(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> Wallet:
    now = now or datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(LoyaltyLedger, Order.status)
            .outerjoin(Order, LoyaltyLedger.order_id == Order.id)
            .where(LoyaltyLedger.user_id == user_id)
            .order_by(LoyaltyLedger.created_at, LoyaltyLedger.id)
        )
    ).all()

    lots: list[Lot] = []
    for entry, order_status in rows:
        at = entry.created_at
        credit = entry.transaction_type == TransactionType.EARNED or (
            entry.transaction_type == TransactionType.ADJUSTMENT and entry.amount > 0
        )
        if credit:
            if order_status in VOID_STATUSES:
                continue  # points earned on an order that was later cancelled are void
            lots.append(Lot(entry.id, entry.source, entry.amount, entry.amount, at, entry.expires_at))
            continue
        if entry.transaction_type == TransactionType.SPENT:
            if order_status in VOID_STATUSES:
                continue  # spending on a cancelled (or unpaid) order is void
            debit = entry.amount
        elif entry.transaction_type == TransactionType.ADJUSTMENT:
            debit = -entry.amount
        else:
            continue  # EXPIRED rows are informational; expiry is derived
        # Spend the soonest-expiring points that were still valid at that moment.
        for lot in sorted(
            (lot for lot in lots if lot.remaining > 0 and (lot.expires_at is None or lot.expires_at > at)),
            key=lambda lot: (lot.expires_at is None, lot.expires_at or at),
        ):
            used = min(lot.remaining, debit)
            lot.remaining -= used
            debit -= used
            if debit == 0:
                break

    live = sorted(
        (lot for lot in lots if lot.remaining > 0 and (lot.expires_at is None or lot.expires_at > now)),
        key=lambda lot: (lot.expires_at is None, lot.expires_at or now),
    )
    return Wallet(
        balance=sum(lot.remaining for lot in live),
        lots=live,
        expiring_soon=sum(
            lot.remaining for lot in live if lot.expires_at is not None and lot.expires_at <= now + EXPIRY_WARNING
        ),
    )


async def _last_bonus(db: AsyncSession, user_id: uuid.UUID) -> datetime | None:
    """When the last annual bonus (or Kumbarapuan, its predecessor) was granted.

    A bonus whose order was later cancelled is void in the wallet, and is
    ignored here too: the spending it was for counts again toward the next one.
    """
    return await db.scalar(
        select(func.max(LoyaltyLedger.created_at))
        .outerjoin(Order, LoyaltyLedger.order_id == Order.id)
        .where(
            LoyaltyLedger.user_id == user_id,
            LoyaltyLedger.source == PointSource.KUMBARA,
            LoyaltyLedger.transaction_type == TransactionType.EARNED,
            (Order.status.is_(None)) | (Order.status.not_in(VOID_STATUSES)),
        )
    )


async def milestone_progress(
    db: AsyncSession,
    user_id: uuid.UUID,
    loyalty: LoyaltyParameters = DEFAULT_LOYALTY,
    now: datetime | None = None,
) -> MilestoneProgress:
    """The customer's paid spending toward the annual bonus.

    One indexed SUM over the customer's paid orders since the later of
    "`annual_period_days` ago" and their last bonus — so spending that earned a
    bonus never counts toward another, and spending older than the window drops
    out of it. Orders at or above `annual_order_ceiling` are left out.
    """
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=loyalty.annual_period_days)
    last = await _last_bonus(db, user_id)
    restarted = last is not None and last > window_start
    since = last if restarted else window_start
    stmt = select(func.coalesce(func.sum(Order.total_amount), 0)).where(
        Order.user_id == user_id,
        Order.status.in_(PAID_STATUSES),
        Order.paid_at > since,
        Order.paid_at <= now,
    )
    if loyalty.annual_order_ceiling:
        stmt = stmt.where(Order.total_amount < loyalty.annual_order_ceiling)
    spent = await db.scalar(stmt)
    return MilestoneProgress(
        spent=Decimal(spent).quantize(Decimal("0.01")),
        target=loyalty.annual_spend_threshold,
        bonus_points=loyalty.annual_bonus_points,
        period_days=loyalty.annual_period_days,
        counting_since=since,
        restarted=restarted,
    )


async def award_for_paid_order(db: AsyncSession, order: Order, now: datetime) -> tuple[int, int]:
    """Grant Sultan Puan for an order that was just marked paid (order.paid_at =
    now, already flushed). Returns (standard, annual bonus) granted. Idempotent
    per order.

    The rules are the live store parameters: nothing at all while the program
    is switched off; the standard award for an order inside the band; and the
    annual bonus when this order brings the rolling spend to the target.

    Points are a retail promotion, so a B2B account earns none — see
    ``app.services.perks``. Loading the buyer here rather than taking it as an
    argument keeps every caller (the admin API, the SLA job) out of the rule.
    """
    loyalty = (await load_parameters(db)).loyalty
    if not loyalty.enabled:
        return 0, 0
    buyer = await db.scalar(
        select(User).where(User.id == order.user_id).options(selectinload(User.segment))
    )
    if buyer is None or not retail_perks(buyer, buyer.segment.code if buyer.segment else None):
        return 0, 0

    already = await db.scalar(
        select(LoyaltyLedger.id).where(
            LoyaltyLedger.order_id == order.id, LoyaltyLedger.transaction_type == TransactionType.EARNED
        )
    )
    if already is not None:
        return 0, 0

    standard = loyalty.points_for(order.total_amount)
    if standard:
        rate = f"{loyalty.base_rate * 100:.2f}".rstrip("0").rstrip(".")
        db.add(LoyaltyLedger(
            user_id=order.user_id, order_id=order.id, amount=standard,
            transaction_type=TransactionType.EARNED, source=PointSource.PARAPUAN,
            created_at=now, expires_at=now + POINT_LIFETIME,
            note=f"%{rate} Sultan Puan · {order.order_number}",
        ))

    bonus = 0
    if loyalty.annual_bonus_points and loyalty.counts_toward_milestone(order.total_amount):
        progress = await milestone_progress(db, order.user_id, loyalty, now)  # includes this order
        if progress.spent >= progress.target:
            bonus = loyalty.annual_bonus_points
            db.add(LoyaltyLedger(
                user_id=order.user_id, order_id=order.id, amount=bonus,
                transaction_type=TransactionType.EARNED, source=PointSource.KUMBARA,
                created_at=now, expires_at=now + POINT_LIFETIME,
                note=f"Yıllık Sultan Puan bonusu · son {loyalty.annual_period_days} günde {format_tl(progress.spent)}",
            ))
    return standard, bonus
