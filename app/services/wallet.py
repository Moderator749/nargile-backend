"""Point wallet (ParaPuan + Kumbarapuan): balance, expiry, and awards.

1 point = 1.00 TRY. Points expire one year after they are earned. The ledger is
append-only; the balance is derived by replaying it in order, with spending
consuming the soonest-expiring points first (FIFO by expiry). A row whose order was
cancelled is skipped, so points spent on it come back with their original expiry
and points earned on it (an approved order that was later cancelled) are void.
"""

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.services.perks import retail_perks

POINT_LIFETIME = timedelta(days=365)
EXPIRY_WARNING = timedelta(days=30)

# ParaPuan: a single paid order in this band earns 10% of its subtotal.
PARAPUAN_MIN = Decimal("20000.00")
PARAPUAN_MAX = Decimal("29999.99")
PARAPUAN_RATE = Decimal("0.10")
# Kumbarapuan: paid orders below 20,000 accumulate; at 20,000 they earn 5%.
KUMBARA_ORDER_CEILING = Decimal("20000.00")
KUMBARA_TARGET = Decimal("20000.00")
KUMBARA_RATE = Decimal("0.05")

PAID_STATUSES = (OrderStatus.APPROVED, OrderStatus.SHIPPED)


def parapuan_for(subtotal: Decimal) -> int:
    if PARAPUAN_MIN <= subtotal <= PARAPUAN_MAX:
        return int((subtotal * PARAPUAN_RATE).quantize(Decimal("1")))
    return 0


@dataclass
class Lot:
    entry_id: uuid.UUID
    source: str | None
    amount: int
    remaining: int
    created_at: datetime
    expires_at: datetime | None


@dataclass
class KumbaraProgress:
    accumulated: Decimal  # paid sub-20k order totals since the last Kumbarapuan grant
    target: Decimal = KUMBARA_TARGET
    rate: Decimal = KUMBARA_RATE


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
            if order_status == OrderStatus.CANCELLED:
                continue  # points earned on an order that was later cancelled are void
            lots.append(Lot(entry.id, entry.source, entry.amount, entry.amount, at, entry.expires_at))
            continue
        if entry.transaction_type == TransactionType.SPENT:
            if order_status == OrderStatus.CANCELLED:
                continue  # spending on a cancelled order is void
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


async def _last_kumbara_grant(db: AsyncSession, user_id: uuid.UUID) -> datetime | None:
    return await db.scalar(
        select(func.max(LoyaltyLedger.created_at)).where(
            LoyaltyLedger.user_id == user_id, LoyaltyLedger.source == PointSource.KUMBARA
        )
    )


async def kumbara_progress(db: AsyncSession, user_id: uuid.UUID) -> KumbaraProgress:
    since = await _last_kumbara_grant(db, user_id)
    stmt = select(func.coalesce(func.sum(Order.total_amount), 0)).where(
        Order.user_id == user_id,
        Order.status.in_(PAID_STATUSES),
        Order.total_amount < KUMBARA_ORDER_CEILING,
        Order.paid_at.is_not(None),
    )
    if since is not None:
        stmt = stmt.where(Order.paid_at > since)
    return KumbaraProgress(accumulated=Decimal(await db.scalar(stmt)))


async def award_for_paid_order(db: AsyncSession, order: Order, now: datetime) -> tuple[int, int]:
    """Grant points for an order that was just marked paid (order.paid_at = now,
    already flushed). Returns (parapuan, kumbarapuan) granted. Idempotent per order.

    Points are a retail promotion, so a B2B account earns none — see
    ``app.services.perks``. Loading the buyer here rather than taking it as an
    argument keeps every caller (the admin API, the SLA job) out of the rule.
    """
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

    parapuan = parapuan_for(order.total_amount)
    if parapuan:
        db.add(LoyaltyLedger(
            user_id=order.user_id, order_id=order.id, amount=parapuan,
            transaction_type=TransactionType.EARNED, source=PointSource.PARAPUAN,
            created_at=now, expires_at=now + POINT_LIFETIME,
            note=f"%10 ParaPuan · {order.order_number}",
        ))

    kumbara = 0
    if order.total_amount < KUMBARA_ORDER_CEILING:
        progress = await kumbara_progress(db, order.user_id)  # includes this order
        if progress.accumulated >= KUMBARA_TARGET:
            kumbara = math.floor(progress.accumulated * KUMBARA_RATE)
            db.add(LoyaltyLedger(
                user_id=order.user_id, order_id=order.id, amount=kumbara,
                transaction_type=TransactionType.EARNED, source=PointSource.KUMBARA,
                created_at=now, expires_at=now + POINT_LIFETIME,
                note=f"%5 Kumbarapuan · ₺{progress.accumulated:,.2f} birikim",
            ))
    return parapuan, kumbara
