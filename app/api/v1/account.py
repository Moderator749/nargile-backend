"""The signed-in customer's wallet, profile, password, addresses and B2B
application; public bank accounts."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.countries import TURKEY
from app.core.database import get_db
from app.core.security import (
    ADMIN_PASSWORD_MIN,
    CUSTOMER_PASSWORD_MIN,
    create_session_token,
    hash_password,
    verify_password,
)
from app.models.b2b import ApplicationStatus, B2BApplication
from app.models.bank_account import BankAccount
from app.models.loyalty import LoyaltyLedger, TransactionType
from app.models.order import VOID_STATUSES, Order
from app.models.user import (
    AccountType,
    AddressKind,
    BillingEntity,
    CustomerAddress,
    CustomerProfile,
    SHIPPING_ADDRESS_LIMIT,
    User,
    UserRole,
)
from app.schemas.address import AddressBookInput, InvoiceAddress, address_line, international_line
from app.schemas.b2b import B2BApplicationCreate, B2BApplicationRead
from app.schemas.communication import CommunicationPreferences
from app.schemas.customer import AddressBookEntry, SavedAddress
from app.schemas.invoice import CorporateInvoiceDetails
from app.schemas.user import PasswordChange, ProfileUpdate, Token
from app.schemas.wallet import BankAccountRead, MilestoneRead, WalletEntry, WalletLot, WalletRead
from app.services.contact_phone import mobile_taken
from app.services.store_parameters import load_parameters
from app.services.wallet import get_wallet, milestone_progress

router = APIRouter(tags=["account"])


async def wallet_read(db: AsyncSession, user_id) -> WalletRead:
    now = datetime.now(timezone.utc)
    wallet = await get_wallet(db, user_id, now)
    loyalty = (await load_parameters(db)).loyalty
    progress = await milestone_progress(db, user_id, loyalty, now)
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
        enabled=loyalty.enabled,
        conversion_rate=loyalty.conversion_rate,
        lots=[
            WalletLot(source=l.source, amount=l.amount, remaining=l.remaining, earned_at=l.created_at, expires_at=l.expires_at)
            for l in wallet.lots
        ],
        milestone=MilestoneRead(
            spent=progress.spent,
            target=progress.target,
            remaining=progress.remaining,
            bonus_points=progress.bonus_points,
            period_days=progress.period_days,
            order_ceiling=loyalty.annual_order_ceiling,
            counting_since=progress.counting_since,
            restarted=progress.restarted,
        ),
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
                void=e.transaction_type == TransactionType.SPENT and status in VOID_STATUSES,
            )
            for e, number, status in rows
        ],
    )


@router.get("/me/wallet", response_model=WalletRead)
async def my_wallet(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Sultan Puan balance, expiry, annual-bonus progress and history."""
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


LABELS = {
    AddressKind.BILLING: "Fatura Adresi",
    AddressKind.CORPORATE_BILLING: "Kurumsal Fatura Adresi",
    AddressKind.SHIPPING: "Teslimat Adresi",
}


def _label(user: User, kind: AddressKind, own: str | None = None) -> str:
    """What to call the address on the card.

    A firm's own invoice address *is* its corporate one, so it is named that way
    even though the row is the account's BILLING: "Fatura Adresi" would read as a
    personal address beside it.
    """
    if kind == AddressKind.BILLING and user.account_type != AccountType.INDIVIDUAL:
        return LABELS[AddressKind.CORPORATE_BILLING]
    # A saved delivery address goes by the name the customer gave it.
    if kind == AddressKind.SHIPPING and own:
        return own
    return LABELS.get(kind, "Adres")


def _saved(user: User, row: CustomerAddress) -> SavedAddress:
    """A stored invoice address offered as somewhere to ship to.

    Deliberately loose: it prefills the checkout form rather than standing in
    for it. An account from before the current sign-up form may have no name or
    number on file, and an address worth offering should not be withheld over a
    field the customer is about to see and can fill in.
    """
    profile = user.profile
    company = profile.company_title if profile else None
    domestic = row.country == TURKEY
    return SavedAddress(
        id=row.id,
        kind=AddressKind(row.kind),
        label=_label(user, AddressKind(row.kind), row.label),
        full_name=(company if row.kind == AddressKind.CORPORATE_BILLING else user.full_name)
        or company
        or user.full_name,
        phone=user.phone,
        country=row.country,
        city=row.city,
        district=row.district,
        postal_code=row.postal_code,
        line=address_line(
            neighbourhood=row.neighbourhood,
            street=row.street,
            building_no=row.building_no,
            building_name=row.building_name,
            door_no=row.door_no,
        )
        if domestic
        else international_line(
            address_line1=row.address_line1,
            address_line2=row.address_line2,
            postal_code=row.postal_code,
        ),
    )


@router.get("/me/addresses", response_model=list[SavedAddress])
async def my_addresses(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The addresses on file, to pick from at checkout.

    The customer's own invoice address comes first, since that is the one
    checkout opens on; a "Kurumsal Fatura Adresi" follows it, then the delivery
    addresses saved in "Adreslerim". The rows have no order of their own, so it
    is given here rather than left to the database.

    Checkout never writes one back: an order keeps its own frozen copy, so
    shipping somewhere once does not quietly change what is on file.
    """
    loaded = await db.scalar(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.profile), selectinload(User.addresses))
    )
    return [_saved(loaded, row) for row in _in_order(loaded.addresses)]


_KIND_ORDER = {AddressKind.BILLING: 0, AddressKind.CORPORATE_BILLING: 1, AddressKind.SHIPPING: 2}


def _in_order(rows: list[CustomerAddress]) -> list[CustomerAddress]:
    """Invoice address, the firm's, then the saved delivery addresses as added."""
    return sorted(rows, key=lambda row: (_KIND_ORDER[AddressKind(row.kind)], row.id))


# ── Fatura bilgileri ────────────────────────────────────────────────────────
#
# Checkout shows what is on file and lets the customer correct it there
# ("Düzenle"), which is the only way these rows change: placing an order never
# writes back by itself, because one order's invoice is not a decision about the
# account. What is saved here is what the next cart starts from.


def _as_line(address: InvoiceAddress) -> dict:
    """An invoice address as a stored row's fields.

    The cart asks for the address as one line, so that line is what the row
    keeps — in ``street``, the field the single-line form of a stored address is
    built from. The parts sign-up asks for separately (bina, kapı) are cleared
    rather than left to contradict it.
    """
    return {
        "country": TURKEY,
        "city": address.city,
        "district": address.district,
        "street": address.line,
        "neighbourhood": None,
        "building_no": None,
        "building_name": None,
        "door_no": None,
        "postal_code": None,
        "address_line1": None,
        "address_line2": None,
    }


async def _upsert_address(db: AsyncSession, user: User, kind: AddressKind, address: InvoiceAddress) -> None:
    row = await db.scalar(
        select(CustomerAddress).where(CustomerAddress.user_id == user.id, CustomerAddress.kind == kind)
    )
    if row is None:
        row = CustomerAddress(user_id=user.id, kind=kind)
        db.add(row)
    for field, value in _as_line(address).items():
        setattr(row, field, value)


async def _profile(db: AsyncSession, user: User) -> CustomerProfile:
    profile = await db.scalar(select(CustomerProfile).where(CustomerProfile.user_id == user.id))
    if profile is None:
        profile = CustomerProfile(user_id=user.id)
        db.add(profile)
    return profile


@router.get("/me/corporate-invoice", response_model=CorporateInvoiceDetails | None)
async def my_corporate_invoice(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """The firm this customer has us invoice, or null when there is none yet.

    Null is also the answer when the details are half there — an account that
    has a firm's address but no tax number has nothing the cart could prefill a
    complete invoice from, and the cart asks for all of it anyway.
    """
    loaded = await db.scalar(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.profile), selectinload(User.addresses))
    )
    profile = loaded.profile
    row = next(
        (a for a in loaded.addresses if a.kind == AddressKind.CORPORATE_BILLING and a.country == TURKEY),
        None,
    )
    if profile is None or row is None or not profile.company_title or not profile.tax_number:
        return None
    if not row.district or not profile.tax_office:
        return None
    return CorporateInvoiceDetails(
        company_title=profile.company_title,
        trade_name=profile.trade_name,
        address=InvoiceAddress(
            city=row.city,
            district=row.district,
            line=address_line(
                neighbourhood=row.neighbourhood,
                street=row.street,
                building_no=row.building_no,
                building_name=row.building_name,
                door_no=row.door_no,
            ),
        ),
        tax_office=profile.tax_office,
        tax_number=profile.tax_number,
    )


@router.put("/me/corporate-invoice", response_model=CorporateInvoiceDetails)
async def save_my_corporate_invoice(
    payload: CorporateInvoiceDetails,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Keep this firm on file, so the next order is already filled in.

    Only an individual has this to say: a business account is invoiced to its
    own firm, and its sign-up details are not something checkout may rewrite.
    """
    if user.account_type != AccountType.INDIVIDUAL:
        raise HTTPException(
            status_code=403,
            detail="Kurumsal hesapların fatura bilgileri kayıt sırasında alınır",
        )
    profile = await _profile(db, user)
    profile.company_title = payload.company_title
    profile.trade_name = payload.trade_name
    profile.tax_office = payload.tax_office
    profile.tax_number = payload.tax_number
    # A TCKN is a person's number, so it is a şahıs firması; ten digits is a
    # company's. The customer never has to answer a question we can read.
    profile.billing_entity_type = (
        BillingEntity.SOLE_PROPRIETOR if len(payload.tax_number) == 11 else BillingEntity.COMPANY
    )
    await _upsert_address(db, user, AddressKind.CORPORATE_BILLING, payload.address)
    await db.commit()
    return payload


@router.put("/me/billing-address", response_model=InvoiceAddress)
async def save_my_billing_address(
    payload: InvoiceAddress,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The customer's own invoice address, corrected at checkout.

    An individual's to change: a firm is invoiced where it is registered, and
    that address is the one its prices and its paperwork hang off.
    """
    if user.account_type != AccountType.INDIVIDUAL:
        raise HTTPException(
            status_code=403, detail="Kurumsal hesapların fatura adresi kayıt sırasında alınır"
        )
    await _upsert_address(db, user, AddressKind.BILLING, payload)
    await db.commit()
    return payload


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


# ── Ticari elektronik ileti onayı ────────────────────────────────────────────


@router.get("/me/communication-preferences", response_model=CommunicationPreferences)
async def get_communication_preferences(user: User = Depends(get_current_user)):
    return CommunicationPreferences(
        email=user.marketing_email_consent_at is not None, sms=user.marketing_sms_consent_at is not None
    )


@router.put("/me/communication-preferences", response_model=CommunicationPreferences)
async def set_communication_preferences(
    payload: CommunicationPreferences,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Consent is dated when given; withdrawing clears it at once."""
    now = datetime.now(timezone.utc)
    if payload.email != (user.marketing_email_consent_at is not None):
        user.marketing_email_consent_at = now if payload.email else None
    if payload.sms != (user.marketing_sms_consent_at is not None):
        user.marketing_sms_consent_at = now if payload.sms else None
    await db.commit()
    return payload


# ── Hesap bilgilerim ─────────────────────────────────────────────────────────


@router.put("/me/profile", response_model=ProfileUpdate)
async def update_my_profile(
    payload: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The name and number the account's orders go out under.

    A Turkish mobile already on another account is refused: "Şifremi unuttum"
    finds an account by its number, and two accounts with one number could
    not be told apart.
    """
    if await mobile_taken(db, payload.phone, user.id):
        raise HTTPException(status_code=409, detail="Bu telefon numarası başka bir hesapta kayıtlı.")
    user.full_name = payload.full_name
    user.phone = payload.phone
    await db.commit()
    return payload


@router.post("/me/password", response_model=Token)
async def change_my_password(
    payload: PasswordChange,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A new password, given with the current one.

    Every other session ends with the old password, as at a reset; this one
    carries on with the fresh token returned.
    """
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Mevcut şifreniz hatalı.")
    minimum = ADMIN_PASSWORD_MIN if user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN) else CUSTOMER_PASSWORD_MIN
    if len(payload.new_password) < minimum:
        raise HTTPException(status_code=422, detail=f"Yeni şifre en az {minimum} karakter olmalı.")
    user.hashed_password = hash_password(payload.new_password)
    user.token_version = (user.token_version or 0) + 1
    await db.commit()
    return Token(access_token=create_session_token(user))


# ── Adreslerim ───────────────────────────────────────────────────────────────
#
# The invoice addresses sign-up took, and the delivery addresses the customer
# keeps beside them. Checkout offers them all as cards and freezes a copy onto
# the order; nothing here changes an order already placed.


def _entry(user: User, row: CustomerAddress) -> AddressBookEntry:
    kind = AddressKind(row.kind)
    shipping = kind == AddressKind.SHIPPING
    own_invoice = kind == AddressKind.BILLING and user.account_type == AccountType.INDIVIDUAL
    return AddressBookEntry(
        **_saved(user, row).model_dump(),
        editable=shipping or own_invoice,
        deletable=shipping,
    )


def _write_place(row: CustomerAddress, payload: AddressBookInput) -> None:
    """The form's one-line address into the row, in the shape its country has
    (see CustomerAddress); the other shape's fields are cleared."""
    domestic = payload.country == TURKEY
    row.country = payload.country
    row.city = payload.city
    row.district = payload.district if domestic else None
    row.street = payload.line if domestic else None
    row.neighbourhood = row.building_no = row.building_name = row.door_no = None
    row.address_line1 = None if domestic else payload.line
    row.address_line2 = None
    row.postal_code = None if domestic else payload.postal_code


async def _book(db: AsyncSession, user: User) -> User:
    return await db.scalar(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.profile), selectinload(User.addresses))
        .execution_options(populate_existing=True)
    )


async def _own_row(db: AsyncSession, user: User, address_id: int) -> CustomerAddress:
    row = await db.scalar(
        select(CustomerAddress).where(CustomerAddress.id == address_id, CustomerAddress.user_id == user.id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Adres bulunamadı")
    return row


@router.get("/me/address-book", response_model=list[AddressBookEntry])
async def my_address_book(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    loaded = await _book(db, user)
    return [_entry(loaded, row) for row in _in_order(loaded.addresses)]


@router.post("/me/address-book", response_model=AddressBookEntry, status_code=201)
async def add_to_address_book(
    payload: AddressBookInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A new delivery address. Only an individual keeps them: a firm's order
    goes to the address it registered (or a branch it names at checkout)."""
    if user.account_type != AccountType.INDIVIDUAL:
        raise HTTPException(status_code=403, detail="Kurumsal hesaplar siparişlerini kayıtlı adreslerine alır.")
    count = await db.scalar(
        select(func.count())
        .select_from(CustomerAddress)
        .where(CustomerAddress.user_id == user.id, CustomerAddress.kind == AddressKind.SHIPPING)
    )
    if count >= SHIPPING_ADDRESS_LIMIT:
        raise HTTPException(status_code=409, detail=f"En fazla {SHIPPING_ADDRESS_LIMIT} teslimat adresi kaydedilebilir.")
    row = CustomerAddress(user_id=user.id, kind=AddressKind.SHIPPING, label=payload.label)
    _write_place(row, payload)
    db.add(row)
    await db.commit()
    return _entry(await _book(db, user), row)


@router.put("/me/address-book/{address_id}", response_model=AddressBookEntry)
async def edit_address_book_entry(
    address_id: int,
    payload: AddressBookInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A delivery address, or an individual's own invoice address. A firm's
    registered address, and the firm an individual is invoiced to, are edited
    with the invoice details they belong to."""
    row = await _own_row(db, user, address_id)
    entry = _entry(await _book(db, user), row)
    if not entry.editable:
        raise HTTPException(status_code=403, detail="Bu adres fatura bilgileriyle birlikte düzenlenir.")
    _write_place(row, payload)
    if row.kind == AddressKind.SHIPPING:
        row.label = payload.label
    await db.commit()
    return _entry(await _book(db, user), row)


@router.delete("/me/address-book/{address_id}", status_code=204)
async def delete_address_book_entry(
    address_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A delivery address only: an invoice address is what invoices are made out to."""
    row = await _own_row(db, user, address_id)
    if row.kind != AddressKind.SHIPPING:
        raise HTTPException(status_code=403, detail="Fatura adresi silinemez.")
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)
