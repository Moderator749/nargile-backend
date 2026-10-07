"""Admin: customers and their point wallets; company bank accounts (havale/EFT)."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin_user
from app.api.v1.account import application_read, wallet_read
from app.core.database import get_db
from app.models.b2b import ApplicationStatus, B2BApplication
from app.models.bank_account import BankAccount
from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.order import Order
from app.models.user import CompanyProfile, CustomerSegment, SegmentType, User
from app.schemas.b2b import B2BApplicationRead, B2BApprove, B2BReject
from app.schemas.wallet import AdminCustomerRead, BankAccountRead, BankAccountWrite, PointAdjustment, WalletRead
from app.services.wallet import POINT_LIFETIME, get_wallet

router = APIRouter(prefix="/admin", tags=["admin-customers"])


# ── Customers & wallets ──────────────────────────────────────────────────────


@router.get("/customers", response_model=list[AdminCustomerRead])
async def list_customers(
    q: str | None = Query(None, max_length=100, description="Email or name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    orders = select(Order.user_id, func.count().label("n")).group_by(Order.user_id).subquery()
    stmt = (
        select(User, func.coalesce(orders.c.n, 0))
        .outerjoin(orders, orders.c.user_id == User.id)
        .options(selectinload(User.segment))
        .order_by(User.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.email.ilike(pattern), User.full_name.ilike(pattern)))
    rows = (await db.execute(stmt)).all()
    return [
        AdminCustomerRead(
            id=u.id,
            email=u.email,
            full_name=u.full_name,
            segment=u.segment.code if u.segment else None,
            balance=(await get_wallet(db, u.id)).balance,
            order_count=n,
        )
        for u, n in rows
    ]


async def _customer(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return user


@router.get("/customers/{user_id}/wallet", response_model=WalletRead)
async def customer_wallet(
    user_id: uuid.UUID, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    await _customer(db, user_id)
    return await wallet_read(db, user_id)


@router.post("/customers/{user_id}/points", response_model=WalletRead)
async def adjust_points(
    user_id: uuid.UUID,
    payload: PointAdjustment,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Add (expires in 1 year) or remove points, with a reason. Can't go below zero."""
    await _customer(db, user_id)
    now = datetime.now(timezone.utc)
    if payload.amount < 0:
        balance = (await get_wallet(db, user_id, now)).balance
        if -payload.amount > balance:
            raise HTTPException(status_code=422, detail=f"Balance is only {balance} points")
    db.add(LoyaltyLedger(
        user_id=user_id,
        amount=payload.amount,
        transaction_type=TransactionType.ADJUSTMENT,
        source=PointSource.ADMIN,
        note=payload.note.strip(),
        created_by=admin.id,
        created_at=now,
        expires_at=now + POINT_LIFETIME if payload.amount > 0 else None,
    ))
    await db.commit()
    return await wallet_read(db, user_id)


# ── Bank accounts ────────────────────────────────────────────────────────────


@router.get("/bank-accounts", response_model=list[BankAccountRead])
async def list_bank_accounts(_admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BankAccount).order_by(BankAccount.sort_order, BankAccount.id))
    return [BankAccountRead.model_validate(a, from_attributes=True) for a in result.scalars()]


async def _iban_free(db: AsyncSession, iban: str, own_id: int | None) -> None:
    clash = await db.scalar(select(BankAccount.id).where(BankAccount.iban == iban, BankAccount.id != own_id))
    if clash is not None:
        raise HTTPException(status_code=409, detail="This IBAN is already listed")


@router.post("/bank-accounts", response_model=BankAccountRead, status_code=status.HTTP_201_CREATED)
async def create_bank_account(
    payload: BankAccountWrite, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    await _iban_free(db, payload.iban, None)
    account = BankAccount(**payload.model_dump())
    db.add(account)
    await db.commit()
    return BankAccountRead.model_validate(account, from_attributes=True)


@router.put("/bank-accounts/{account_id}", response_model=BankAccountRead)
async def update_bank_account(
    account_id: int,
    payload: BankAccountWrite,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    account = await db.get(BankAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    await _iban_free(db, payload.iban, account_id)
    for field, value in payload.model_dump().items():
        setattr(account, field, value)
    await db.commit()
    return BankAccountRead.model_validate(account, from_attributes=True)


@router.delete("/bank-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bank_account(
    account_id: int, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    account = await db.get(BankAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    await db.delete(account)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── B2B applications ─────────────────────────────────────────────────────────


@router.get("/b2b-applications", response_model=list[B2BApplicationRead])
async def list_b2b_applications(
    status_filter: ApplicationStatus | None = Query(ApplicationStatus.PENDING, alias="status"),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(B2BApplication, User).join(User, User.id == B2BApplication.user_id)
    if status_filter is not None:
        stmt = stmt.where(B2BApplication.status == status_filter)
    rows = (await db.execute(stmt.order_by(B2BApplication.created_at.desc()).limit(200))).all()
    return [application_read(a, u) for a, u in rows]


async def _pending_application(db: AsyncSession, application_id: int) -> tuple[B2BApplication, User]:
    row = (
        await db.execute(
            select(B2BApplication, User)
            .join(User, User.id == B2BApplication.user_id)
            .where(B2BApplication.id == application_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    application, user = row
    if application.status != ApplicationStatus.PENDING:
        raise HTTPException(status_code=409, detail="Application was already decided")
    return application, user


@router.post("/b2b-applications/{application_id}/approve", response_model=B2BApplicationRead)
async def approve_b2b_application(
    application_id: int,
    payload: B2BApprove,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Grant the segment (the requested one unless overridden) and record the company."""
    application, user = await _pending_application(db, application_id)
    code = SegmentType(payload.segment or application.requested_segment)
    segment_id = await db.scalar(select(CustomerSegment.id).where(CustomerSegment.code == code))
    user.segment_id = segment_id

    profile = await db.scalar(select(CompanyProfile).where(CompanyProfile.user_id == user.id))
    if profile is None:
        db.add(CompanyProfile(
            user_id=user.id, company_title=application.company_title,
            tax_number=application.tax_number, tax_office=application.tax_office,
        ))
    else:
        profile.company_title = application.company_title
        profile.tax_number = application.tax_number
        profile.tax_office = application.tax_office

    application.status = ApplicationStatus.APPROVED
    application.decided_at = datetime.now(timezone.utc)
    application.decided_by = admin.id
    if code != application.requested_segment:
        application.admin_note = f"{code.value} olarak onaylandı"
    await db.commit()
    return application_read(application, user)


@router.post("/b2b-applications/{application_id}/reject", response_model=B2BApplicationRead)
async def reject_b2b_application(
    application_id: int,
    payload: B2BReject,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    application, user = await _pending_application(db, application_id)
    application.status = ApplicationStatus.REJECTED
    application.admin_note = payload.note.strip()
    application.decided_at = datetime.now(timezone.utc)
    application.decided_by = admin.id
    await db.commit()
    return application_read(application, user)
