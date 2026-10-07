"""The signed-in customer's wallet and saved delivery address; public bank accounts."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.b2b import ApplicationStatus, B2BApplication
from app.models.bank_account import BankAccount
from app.models.loyalty import LoyaltyLedger, TransactionType
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.schemas.b2b import B2BApplicationCreate, B2BApplicationRead
from app.schemas.wallet import BankAccountRead, KumbaraRead, WalletEntry, WalletLot, WalletRead
from app.services.wallet import get_wallet, kumbara_progress

router = APIRouter(tags=["account"])


async def wallet_read(db: AsyncSession, user_id) -> WalletRead:
    now = datetime.now(timezone.utc)
    wallet = await get_wallet(db, user_id, now)
    progress = await kumbara_progress(db, user_id)
    rows = (
        await db.execute(
            select(LoyaltyLedger, Order.order_number, Order.status)
            .outerjoin(Order, LoyaltyLedger.order_id == Order.id)
            .where(LoyaltyLedger.user_id == user_id)
            .order_by(LoyaltyLedger.created_at.desc(), LoyaltyLedger.id)
            .limit(200)
        )
    ).all()

    def signed(e: LoyaltyLedger) -> int:
        if e.transaction_type in (TransactionType.SPENT, TransactionType.EXPIRED):
            return -abs(e.amount)
        return e.amount

    return WalletRead(
        balance=wallet.balance,
        expiring_soon=wallet.expiring_soon,
        lots=[
            WalletLot(source=l.source, amount=l.amount, remaining=l.remaining, earned_at=l.created_at, expires_at=l.expires_at)
            for l in wallet.lots
        ],
        kumbara=KumbaraRead(accumulated=progress.accumulated, target=progress.target, rate=progress.rate),
        history=[
            WalletEntry(
                id=e.id,
                created_at=e.created_at,
                points=signed(e),
                kind=e.transaction_type.value if hasattr(e.transaction_type, "value") else e.transaction_type,
                source=e.source.value if hasattr(e.source, "value") else e.source,
                note=e.note,
                order_number=number,
                expires_at=e.expires_at,
                void=e.transaction_type == TransactionType.SPENT and status == OrderStatus.CANCELLED,
            )
            for e, number, status in rows
        ],
    )


@router.get("/me/wallet", response_model=WalletRead)
async def my_wallet(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """ParaPuan + Kumbarapuan balance, expiry, Kumbara progress and history."""
    return await wallet_read(db, user.id)


@router.get("/me/last-address", response_model=dict | None)
async def my_last_address(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The delivery address of the customer's latest order, to prefill checkout."""
    return await db.scalar(
        select(Order.shipping_address)
        .where(Order.user_id == user.id, Order.shipping_address.is_not(None))
        .order_by(Order.created_at.desc())
        .limit(1)
    )


@router.get("/bank-accounts", response_model=list[BankAccountRead])
async def bank_accounts(db: AsyncSession = Depends(get_db)):
    """Accounts customers pay havale/EFT into (public: shown after checkout)."""
    result = await db.execute(
        select(BankAccount).where(BankAccount.is_active.is_(True)).order_by(BankAccount.sort_order, BankAccount.id)
    )
    return [BankAccountRead.model_validate(a, from_attributes=True) for a in result.scalars()]


@router.get("/me/b2b-application", response_model=B2BApplicationRead | None)
async def my_b2b_application(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The customer's latest Kafe / Toptan / Bayi application, if any."""
    app_row = await db.scalar(
        select(B2BApplication).where(B2BApplication.user_id == user.id).order_by(B2BApplication.created_at.desc()).limit(1)
    )
    return application_read(app_row, user) if app_row else None


@router.post("/me/b2b-application", response_model=B2BApplicationRead, status_code=201)
async def apply_for_b2b(
    payload: B2BApplicationCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Apply for B2B pricing after sign-up. One open application at a time."""
    pending = await db.scalar(
        select(B2BApplication.id).where(
            B2BApplication.user_id == user.id, B2BApplication.status == ApplicationStatus.PENDING
        )
    )
    if pending is not None:
        raise HTTPException(status_code=409, detail="Bekleyen bir başvurunuz zaten var")
    row = B2BApplication(
        user_id=user.id,
        requested_segment=payload.segment,
        company_title=payload.company_title,
        tax_number=payload.tax_number,
        tax_office=payload.tax_office,
    )
    db.add(row)
    await db.commit()
    return application_read(row, user)


def application_read(row: B2BApplication, user: User) -> B2BApplicationRead:
    return B2BApplicationRead(
        id=row.id,
        user_id=row.user_id,
        email=user.email,
        full_name=user.full_name,
        requested_segment=row.requested_segment,
        company_title=row.company_title,
        tax_number=row.tax_number,
        tax_office=row.tax_office,
        status=row.status.value if hasattr(row.status, "value") else row.status,
        admin_note=row.admin_note,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )
