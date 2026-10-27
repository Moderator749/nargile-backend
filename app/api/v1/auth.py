import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.security import create_session_token, hash_password, needs_rehash, verify_password
from app.models.user import (
    AddressKind,
    CustomerAddress,
    CustomerProfile,
    CustomerSegment,
    SegmentType,
    User,
    UserRole,
)
from app.schemas.address import InternationalAddress, StructuredAddress
from app.schemas.registration import RegistrationCreate
from app.schemas.user import LoginRequest, Token, UserMe, UserRead
from app.services.b2b_intake import application_for
from app.services.loyalty_service import get_user_loyalty_balance
from app.services.marketing_consent import prompt_due

router = APIRouter(prefix="/auth", tags=["auth"])


def _address(
    user_id: uuid.UUID, kind: AddressKind, address: StructuredAddress | InternationalAddress
) -> CustomerAddress:
    """Both shapes are a subset of the same columns, and both name their country,
    so the row says which of the two it is without being told."""
    return CustomerAddress(user_id=user_id, kind=kind, **address.model_dump())


def _profile(user_id: uuid.UUID, payload: RegistrationCreate) -> CustomerProfile:
    """The sign-up form minus the login: who they are and who to invoice. One row
    per self-registered account, however little of it applies."""
    company = payload.company
    return CustomerProfile(
        user_id=user_id,
        national_id=payload.national_id,
        international_kind=payload.international_kind,
        contact_name=payload.contact_name,
        billing_entity_type=company.entity_type if company else None,
        company_title=company.title if company else None,
        tax_plate_name=company.tax_plate_name if company else None,
        tax_office=company.tax_office if company else None,
        tax_number=company.tax_number if company else None,
    )


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: RegistrationCreate, db: AsyncSession = Depends(get_db)):
    """Create an account of one of the three kinds. See
    :class:`~app.schemas.registration.RegistrationCreate` for what each needs."""
    if await db.scalar(select(User.id).where(func.lower(User.email) == payload.email)):
        raise HTTPException(status_code=409, detail="Email already registered")
    if payload.username and await db.scalar(
        select(User.id).where(func.lower(User.username) == payload.username.lower())
    ):
        raise HTTPException(status_code=409, detail="Username already taken")

    # Self-service sign-ups are always RETAIL, whatever kind of account it is.
    # The B2B segments (CAFE/WHOLESALE/DEALER) unlock lower prices, so they are
    # an admin's to grant — from the queue a firm joins the moment it registers.
    retail_id = await db.scalar(
        select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL)
    )
    if retail_id is None:
        raise HTTPException(status_code=500, detail="Default segment not configured")

    # One box on the form covers both channels; the moment it was ticked is
    # what the consent record is, so the server's clock stamps it.
    consented_at = datetime.now(timezone.utc) if payload.marketing_consent else None
    user = User(
        email=payload.email,
        username=payload.username,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        phone=payload.phone,
        landline_phone=payload.landline_phone,
        account_type=payload.account_type,
        segment_id=retail_id,
        marketing_email_consent_at=consented_at,
        marketing_sms_consent_at=consented_at,
        # The sign-up form asks, so the one-time card never has to.
        consent_prompted_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()  # user.id for everything hanging off it

    db.add(_profile(user.id, payload))
    if payload.billing_address is not None:
        db.add(_address(user.id, AddressKind.BILLING, payload.billing_address))
    if payload.company is not None and payload.company.address is not None:
        db.add(_address(user.id, AddressKind.CORPORATE_BILLING, payload.company.address))

    # A firm puts itself in the approval queue by registering: it should never
    # be left trading at retail with nobody aware of it.
    application = application_for(user.id, payload)
    if application is not None:
        db.add(application)

    await db.commit()
    await db.refresh(user)
    return user


# Failed logins per account name per window, so guessing a password — six
# characters is enough for a customer — stays slow. Counted per name, not per
# client address: the API sits behind the storefront's proxy, where one
# address is many shoppers. A success clears the count. In-process: one per
# worker, which is enough to blunt a script.
LOGIN_FAILURES, LOGIN_WINDOW = 10, 15 * 60
_failures: dict[str, deque[float]] = defaultdict(deque)


def _recent_failures(name: str, now: float) -> deque[float]:
    failures = _failures[name]
    while failures and now - failures[0] > LOGIN_WINDOW:
        failures.popleft()
    return failures


@router.post("/login", response_model=Token)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Either the e-mail address or the username, both matched case-insensitively."""
    given = payload.email.strip().lower()
    now = time.monotonic()
    failures = _recent_failures(given, now)
    if len(failures) >= LOGIN_FAILURES:
        raise HTTPException(
            status_code=429,
            detail="Çok fazla hatalı giriş denemesi. Lütfen 15 dakika sonra tekrar deneyin.",
        )
    result = await db.execute(
        select(User)
        .where(or_(func.lower(User.email) == given, func.lower(User.username) == given))
        .order_by(User.created_at)
        .limit(1)
    )
    user = result.scalars().first()
    if not user or not verify_password(payload.password, user.hashed_password):
        failures.append(now)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    _failures.pop(given, None)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    # An account from the old store signs in with its OpenCart password once;
    # from then on it has a bcrypt hash like everyone else.
    if needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(payload.password)
        await db.commit()

    return Token(access_token=create_session_token(user))


@router.post("/refresh", response_model=Token)
async def refresh_session(user: User = Depends(get_current_user)):
    """A fresh token for a session still alive: the storefront asks on each
    visit, so a session ends only after a whole lifetime without one (30 days
    for a customer, a day for the panel's staff). An expired or ended token
    cannot be refreshed — that is a new login."""
    return Token(access_token=create_session_token(user))


@router.get("/me", response_model=UserMe)
async def read_me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Fetch the code explicitly: lazy-loading user.segment fails under AsyncSession.
    segment_code = None
    if user.segment_id is not None:
        segment_code = await db.scalar(
            select(CustomerSegment.code).where(CustomerSegment.id == user.segment_id)
        )
    return UserMe.model_validate(user).model_copy(
        update={
            "segment_code": segment_code,
            "loyalty_balance": await get_user_loyalty_balance(db, user.id),
            "marketing_email_consent": user.marketing_email_consent_at is not None,
            "marketing_sms_consent": user.marketing_sms_consent_at is not None,
            "consent_prompt_due": user.role == UserRole.CUSTOMER and prompt_due(user),
        }
    )
