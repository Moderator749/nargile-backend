"""Admin: customers — the grid, one customer in full, their price list and their
point wallet — plus the company bank accounts (havale/EFT) and the B2B queue."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin_user
from app.api.v1.account import application_read, wallet_read
from app.core.database import get_db
from app.models.b2b import ApplicationStatus, B2BApplication
from app.models.bank_account import BankAccount
from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.order import Order
from app.models.user import (
    AccountType,
    AddressKind,
    CustomerAddress,
    CustomerProfile,
    CustomerSegment,
    SegmentType,
    User,
)
from app.schemas.admin import SortDirection
from app.schemas.b2b import B2BApplicationRead, B2BApprove, B2BReject
from app.schemas.customer import (
    AdminCustomerDetail,
    AdminCustomerRead,
    CustomerAddressRead,
    CustomerProfileRead,
    CustomerSort,
    SegmentAssign,
)
from app.schemas.wallet import BankAccountRead, BankAccountWrite, PointAdjustment, WalletRead
from app.services.loyalty_service import get_user_loyalty_balance
from app.services.wallet import POINT_LIFETIME, get_wallet

router = APIRouter(prefix="/admin", tags=["admin-customers"])


# ── Customers & wallets ──────────────────────────────────────────────────────


@router.get("/customers", response_model=list[AdminCustomerRead])
async def list_customers(
    response: Response,
    q: str | None = Query(None, max_length=100, description="Email, name or company title"),
    account_type: AccountType | None = Query(None),
    segment: SegmentType | None = Query(None),
    order_by: CustomerSort | None = Query(None, description="name | created | orders"),
    order_dir: SortDirection = Query(SortDirection.ASC),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """The customers grid: who registered, as what, where, on which price list.

    Newest first unless asked otherwise, because the newest account is the one
    waiting to be looked at. The X-Total-Count header carries the number of
    matches, for page controls.
    """
    orders = select(Order.user_id, func.count().label("n")).group_by(Order.user_id).subquery()
    # One address per customer, their own invoice address for preference: the
    # grid shows where they are, and DISTINCT ON picks it in the same scan
    # rather than a query per row.
    places = (
        select(
            CustomerAddress.user_id.label("user_id"),
            CustomerAddress.city.label("city"),
            CustomerAddress.district.label("district"),
            CustomerAddress.country.label("country"),
        )
        .ext(distinct_on(CustomerAddress.user_id))
        .order_by(
            CustomerAddress.user_id,
            case((CustomerAddress.kind == AddressKind.BILLING, 0), else_=1),
        )
        .subquery()
    )
    filters = []
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                User.email.ilike(pattern),
                User.full_name.ilike(pattern),
                CustomerProfile.company_title.ilike(pattern),
            )
        )
    if account_type is not None:
        filters.append(User.account_type == account_type)
    if segment is not None:
        filters.append(CustomerSegment.code == segment)

    # Outer joins throughout: an account has a profile only if it registered
    # through the current form, and a segment only if one was assigned.
    base = (
        select(
            User,
            CustomerProfile.company_title,
            func.coalesce(orders.c.n, 0),
            places.c.city,
            places.c.district,
            places.c.country,
        )
        .outerjoin(CustomerProfile, CustomerProfile.user_id == User.id)
        .outerjoin(CustomerSegment, CustomerSegment.id == User.segment_id)
        .outerjoin(orders, orders.c.user_id == User.id)
        .outerjoin(places, places.c.user_id == User.id)
        .where(*filters)
    )
    response.headers["X-Total-Count"] = str(
        await db.scalar(select(func.count()).select_from(base.subquery()))
    )

    descending = order_dir is SortDirection.DESC
    columns = {
        # A firm is known by its unvan, a person by their name; sort by whichever
        # the row shows.
        CustomerSort.NAME: func.coalesce(CustomerProfile.company_title, User.full_name, User.email),
        CustomerSort.CREATED: User.created_at,
        CustomerSort.ORDERS: func.coalesce(orders.c.n, 0),
    }
    column = columns[order_by] if order_by else User.created_at
    primary = column.desc() if (descending or order_by is None) else column.asc()
    rows = (
        await db.execute(
            base.options(selectinload(User.segment))
            .order_by(primary, User.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
    ).all()
    return [
        AdminCustomerRead(
            id=u.id,
            email=u.email,
            full_name=u.full_name,
            company_title=title,
            account_type=u.account_type,
            phone=u.phone,
            segment=u.segment.code if u.segment else None,
            order_count=n,
            created_at=u.created_at,
            city=city,
            district=district,
            country=country,
        )
        for u, title, n, city, district, country in rows
    ]


async def _customer(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return user


async def _detail(db: AsyncSession, user_id: uuid.UUID) -> AdminCustomerDetail:
    """One customer in full — everything sign-up collected, plus what they have
    bought and where they stand on a price list."""
    user = await db.scalar(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.segment), selectinload(User.profile), selectinload(User.addresses))
    )
    if user is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    order_count = await db.scalar(
        select(func.count()).select_from(Order).where(Order.user_id == user_id)
    )
    application = await db.scalar(
        select(B2BApplication)
        .where(B2BApplication.user_id == user_id)
        .order_by(B2BApplication.created_at.desc())
        .limit(1)
    )
    return AdminCustomerDetail(
        id=user.id,
        email=user.email,
        username=user.username,
        full_name=user.full_name,
        phone=user.phone,
        landline_phone=user.landline_phone,
        account_type=user.account_type,
        is_active=user.is_active,
        is_verified=user.is_verified,
        created_at=user.created_at,
        segment=user.segment.code if user.segment else None,
        order_count=order_count or 0,
        loyalty_balance=await get_user_loyalty_balance(db, user_id),
        profile=CustomerProfileRead.model_validate(user.profile) if user.profile else None,
        addresses=[CustomerAddressRead.model_validate(a) for a in user.addresses],
        application_status=(
            application.status.value if hasattr(application.status, "value") else application.status
        )
        if application
        else None,
        requested_segment=application.requested_segment if application else None,
    )


@router.get("/customers/{user_id}", response_model=AdminCustomerDetail)
async def customer_detail(
    user_id: uuid.UUID,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await _detail(db, user_id)


@router.put("/customers/{user_id}/segment", response_model=AdminCustomerDetail)
async def assign_segment(
    user_id: uuid.UUID,
    payload: SegmentAssign,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Put a customer on a price list.

    Sign-up never grants a tier, so this is how a kafe or a bayi gets theirs —
    directly, or after approving their application. It takes effect on their
    next request: prices are read from the account's segment every time, never
    cached into their token.
    """
    user = await _customer(db, user_id)
    segment_id = await db.scalar(
        select(CustomerSegment.id).where(CustomerSegment.code == payload.segment)
    )
    if segment_id is None:
        raise HTTPException(status_code=500, detail="Segment not configured")
    user.segment_id = segment_id
    await db.commit()
    return await _detail(db, user_id)


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

    # The approved company goes on the customer's profile, which is where
    # invoices are addressed from. A business account filled it in at sign-up
    # and the application is a copy of it, so this is a no-op for them; an
    # individual who applied later gets theirs from the application.
    profile = await db.scalar(select(CustomerProfile).where(CustomerProfile.user_id == user.id))
    if profile is None:
        profile = CustomerProfile(user_id=user.id)
        db.add(profile)
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
