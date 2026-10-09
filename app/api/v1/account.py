"""The signed-in customer's wallet, their saved addresses, their B2B
application; public bank accounts."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.countries import TURKEY
from app.core.database import get_db
from app.models.b2b import ApplicationStatus, B2BApplication
from app.models.bank_account import BankAccount
from app.models.loyalty import LoyaltyLedger, TransactionType
from app.models.order import Order, OrderStatus
from app.models.user import (
    AccountType,
    AddressKind,
    BillingEntity,
    CustomerAddress,
    CustomerProfile,
    User,
)
from app.schemas.address import InvoiceAddress, address_line, international_line
from app.schemas.b2b import B2BApplicationCreate, B2BApplicationRead
from app.schemas.customer import SavedAddress
from app.schemas.invoice import CorporateInvoiceDetails
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


LABELS = {
    AddressKind.BILLING: "Fatura Adresi",
    AddressKind.CORPORATE_BILLING: "Kurumsal Fatura Adresi",
}


def _label(user: User, kind: AddressKind) -> str:
    """What to call the address on the card.

    A firm's own invoice address *is* its corporate one, so it is named that way
    even though the row is the account's BILLING: "Fatura Adresi" would read as a
    personal address beside it.
    """
    if kind == AddressKind.BILLING and user.account_type != AccountType.INDIVIDUAL:
        return LABELS[AddressKind.CORPORATE_BILLING]
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
        kind=AddressKind(row.kind),
        label=_label(user, AddressKind(row.kind)),
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
    """The addresses the customer gave at sign-up, to pick from at checkout.

    The customer's own invoice address comes first, since that is the one
    checkout opens on; a "Kurumsal Fatura Adresi" follows it. The rows have no
    order of their own, so it is given here rather than left to the database.

    Checkout never writes one back: an order keeps its own frozen copy, so
    shipping somewhere once does not quietly change what is on file.
    """
    loaded = await db.scalar(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.profile), selectinload(User.addresses))
    )
    rows = sorted(loaded.addresses, key=lambda row: row.kind != AddressKind.BILLING)
    return [_saved(loaded, row) for row in rows]


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
