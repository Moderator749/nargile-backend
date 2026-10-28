import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SegmentType(str, enum.Enum):
    RETAIL = "RETAIL"
    CAFE = "CAFE"
    WHOLESALE = "WHOLESALE"
    DEALER = "DEALER"


class UserRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    STAFF = "STAFF"
    CUSTOMER = "CUSTOMER"


class AccountType(str, enum.Enum):
    """What kind of customer signed up — the first choice on the sign-up form.

    It is their line of business, not their legal form: a kafe and a tobacco
    shop buy different things and are approved on different documents, while
    whether either is a şahıs firması or a şirket is a question about the
    invoice and is asked as :class:`BillingEntity` further down the form.

    It decides which fields the form asks for; it says nothing about pricing,
    which is the segment's job and an admin's decision.
    """

    INDIVIDUAL = "INDIVIDUAL"  # Bireysel
    CAFE = "CAFE"  # Kafe & Restoran
    TOBACCO_SHOP = "TOBACCO_SHOP"  # Nargile & Tobacco Shop
    # Yurt dışı: a customer outside Turkey, business or private. Their identity
    # numbers and their address follow their own country's rules, not ours, so
    # none of the Turkish checks apply to them.
    INTERNATIONAL = "INTERNATIONAL"
    # Written while the two business cards asked for the legal form instead of
    # the line of business. Still accepted from the database, never offered
    # again: what they are is in ``CustomerProfile.billing_entity_type``, and
    # what they sell was never asked.
    SOLE_PROPRIETOR = "SOLE_PROPRIETOR"
    COMPANY = "COMPANY"


#: The four cards the sign-up form offers. The legacy pair above is not among them.
SELECTABLE_ACCOUNT_TYPES = frozenset({
    AccountType.INDIVIDUAL,
    AccountType.CAFE,
    AccountType.TOBACCO_SHOP,
    AccountType.INTERNATIONAL,
})
#: Account types that trade in Turkey and are invoiced as a firm.
TURKISH_BUSINESS_TYPES = frozenset({
    AccountType.CAFE,
    AccountType.TOBACCO_SHOP,
    AccountType.SOLE_PROPRIETOR,
    AccountType.COMPANY,
})


class BillingEntity(str, enum.Enum):
    """Which kind of entity the invoice is made out to — şahıs firması or şirket.

    Always the customer's own answer: a business account picks it in its
    invoice section, and an individual who wants a corporate invoice
    ("Faturayı firmaya istiyorum") picks it there. A sole proprietorship is a
    person, which is why only that answer brings a TC number with it.
    """

    SOLE_PROPRIETOR = "SOLE_PROPRIETOR"
    COMPANY = "COMPANY"


class InternationalKind(str, enum.Enum):
    """What an international customer buys for.

    The counterpart of the two domestic business cards: a customer outside
    Turkey picks it in their own section instead, since one account type covers
    the whole of abroad. Both are businesses — there is no private export
    customer — and it is the first thing the sales team needs to know before
    quoting prices and freight.
    """

    WHOLESALER = "WHOLESALER"  # Toptancı / distributor
    CAFE = "CAFE"  # Kafe / restoran


class AddressKind(str, enum.Enum):
    """Two invoice addresses, one of each at most — sign-up asks where to send
    an invoice, never where to send a parcel — and the customer's own address
    book of places to ship to, kept from "Hesabım". Delivery is still settled
    at checkout, which freezes its own copy onto the order."""

    BILLING = "BILLING"  # Fatura adresi — the account holder's own
    # The address of the firm an individual asked to be invoiced instead. A
    # business account has no such row: its own billing address is its firm's.
    CORPORATE_BILLING = "CORPORATE_BILLING"
    # A delivery address the customer saved ("Ev", "İş"); as many as they like,
    # up to SHIPPING_ADDRESS_LIMIT.
    SHIPPING = "SHIPPING"


#: The invoice kinds, each at most once per account.
INVOICE_ADDRESS_KINDS = ("BILLING", "CORPORATE_BILLING")
SHIPPING_ADDRESS_LIMIT = 10


class CustomerSegment(Base):
    __tablename__ = "customer_segments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        Enum(SegmentType), unique=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    users: Mapped[list["User"]] = relationship(back_populates="segment")


class User(Base):
    """The login itself, plus the handful of fields every account has.

    Everything the sign-up form asks beyond this — tax identity and invoice
    addresses — lives on :class:`CustomerProfile`, so a login or a token check
    never drags it along.
    """

    __tablename__ = "users"
    __table_args__ = (
        # One account per e-mail address; a guest checkout is not an account,
        # so any number of guest rows may carry the same address (each its own
        # order's buyer, none able to see another's).
        Index("ix_users_email", "email", unique=True, postgresql_where=text("NOT is_guest")),
        # Optional, so most rows are NULL; a functional index keeps "Ali" and
        # "ali" from being two accounts while allowing any number of NULLs.
        Index("uq_users_username_lower", text("lower(username)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Kullanıcı adı: an optional second way to log in, stored as the customer
    # typed it and matched case-insensitively.
    username: Mapped[str | None] = mapped_column(String(30))
    full_name: Mapped[str | None] = mapped_column(String(150))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    # Cep telefonu — the number orders and cargo notifications go to. Long
    # enough for an international number with its dialling code and spacing.
    phone: Mapped[str | None] = mapped_column(String(30))
    # Sabit telefon, for business accounts that answer a landline.
    landline_phone: Mapped[str | None] = mapped_column(String(30))
    account_type: Mapped[str] = mapped_column(
        Enum(AccountType), nullable=False, default=AccountType.INDIVIDUAL,
        server_default=AccountType.INDIVIDUAL.value,
    )
    segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_segments.id")
    )
    # Ticari elektronik ileti onayı (6563 sayılı Kanun): when the customer agreed
    # to campaign e-mail / SMS; null = no consent, so no campaign reaches them.
    # Transactional messages (password reset, order notices) need none.
    marketing_email_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    marketing_sms_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the customer was last asked for that consent (sign-up, the one-time
    # "Kampanyalardan haberdar olun" card, checkout's box, the preferences
    # switches): null = never asked, which is every account the old store
    # brought over. The card is shown once, and never again after an answer.
    consent_prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Misafir: the buyer of one guest checkout ("Üye olmadan devam et"), not an
    # account. It cannot sign in (no usable password), is left out of every
    # lookup by e-mail or phone, and its session is short. Setting a password
    # on the order's confirmation turns it into an account (POST /auth/guest/claim).
    is_guest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Carried in every session token ("ver"); raising it ends every session
    # issued before — a password reset does, so a token from before it is dead.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    role: Mapped[str] = mapped_column(
        Enum(UserRole), default=UserRole.CUSTOMER, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    segment: Mapped[CustomerSegment | None] = relationship(back_populates="users")
    profile: Mapped["CustomerProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    addresses: Mapped[list["CustomerAddress"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="CustomerAddress.kind"
    )


class CustomerProfile(Base):
    """Who the customer is, as the sign-up form asks it: an identity and an
    invoice, and nothing about how an order travels.

    One row per account, created at registration. The tax columns carry whoever
    the invoice is made out to — the customer's own firm for a business account,
    or the firm an individual asked to be invoiced instead — which is why
    ``billing_entity_type`` is stored rather than read off the account type.

    An admin approving a Kafe/Toptan/Bayi application writes the application's
    company details here too, so the invoice details have one home.
    """

    __tablename__ = "customer_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    # TC kimlik numarası: optional, and asked of a person — an individual, or
    # the owner of a şahıs firması, who is one.
    national_id: Mapped[str | None] = mapped_column(String(11))

    # ── Fatura kimliği (business accounts, or an individual's corporate invoice)
    billing_entity_type: Mapped[str | None] = mapped_column(Enum(BillingEntity))
    company_title: Mapped[str | None] = mapped_column(String(255))  # Unvan
    # Firma adı: what the firm is called day to day, when that is not its ünvan.
    trade_name: Mapped[str | None] = mapped_column(String(200))
    # The name printed on the tax plate: the owner's for a sole proprietorship,
    # the company's for a company. Not the same thing as the contact person.
    tax_plate_name: Mapped[str | None] = mapped_column(String(150))
    tax_office: Mapped[str | None] = mapped_column(String(100))  # Vergi dairesi
    # VKN (10 digits) or a sole proprietor's TCKN (11). An international
    # customer's VAT/EIN number goes here too, in whatever shape their country
    # issues it — hence the room and the absence of a format.
    tax_number: Mapped[str | None] = mapped_column(String(30))

    # Which kind of international customer this is; NULL for a domestic account,
    # whose line of business is its account type.
    international_kind: Mapped[str | None] = mapped_column(Enum(InternationalKind))

    # ── İletişim ────────────────────────────────────────────────────────────
    # Yetkili kişi: who to call at the firm. Copied to users.full_name as well,
    # so admin screens have a name to show without joining this table.
    contact_name: Mapped[str | None] = mapped_column(String(150))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user: Mapped[User] = relationship(back_populates="profile")


class CustomerAddress(Base):
    """A structured address, field by field as the form asks for it.

    Two shapes share the table, told apart by ``country``: a Turkish address
    (mahalle, cadde, bina, ilçe) and an international one (address lines, postal
    code), because nothing else would fit both a Turkish invoice and a customs
    declaration. The check constraint keeps a row from being half of each.
    ``city`` is the one field both have.

    Kept apart from the profile because an account has one of each invoice kind
    and any number of saved delivery addresses, and because a free-text blob cannot be searched, corrected or printed on an
    invoice reliably. Where an order goes is a separate question, asked at
    checkout and frozen onto the order itself.
    """

    __tablename__ = "customer_addresses"
    __table_args__ = (
        # One invoice address of each kind; saved delivery addresses are many.
        Index(
            "uq_customer_address_kind",
            "user_id",
            "kind",
            unique=True,
            postgresql_where=text("kind IN ('BILLING', 'CORPORATE_BILLING')"),
        ),
        CheckConstraint(
            "CASE WHEN country = 'TR'"
            " THEN district IS NOT NULL AND (neighbourhood IS NOT NULL OR street IS NOT NULL)"
            " ELSE address_line1 IS NOT NULL END",
            name="ck_customer_address_shape",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(Enum(AddressKind), nullable=False)
    # A saved delivery address's name on its card ("Ev", "İş"); unused on an
    # invoice address, whose kind names it.
    label: Mapped[str | None] = mapped_column(String(60))

    # ISO 3166-1 alpha-2; "TR" for a domestic address, which is what every row
    # written before international accounts existed is.
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="TR", server_default="TR")
    # Şehir, or an international city / state / province.
    city: Mapped[str] = mapped_column(String(80), nullable=False)

    # ── Türkiye ─────────────────────────────────────────────────────────────
    neighbourhood: Mapped[str | None] = mapped_column(String(120))  # Mahalle / Köy
    # Cadde / Sokak as the sign-up form asks it — and, when an address is edited
    # at checkout as one free-text line ("Açık Adres"), that whole line, which is
    # why it is as long as an address line rather than as long as a street name.
    street: Mapped[str | None] = mapped_column(String(200))
    building_no: Mapped[str | None] = mapped_column(String(20))  # Bina no
    building_name: Mapped[str | None] = mapped_column(String(120))  # Bina adı
    door_no: Mapped[str | None] = mapped_column(String(20))  # Kapı no
    district: Mapped[str | None] = mapped_column(String(80))  # İlçe

    # ── Yurt dışı ───────────────────────────────────────────────────────────
    postal_code: Mapped[str | None] = mapped_column(String(20))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))

    user: Mapped[User] = relationship(back_populates="addresses")
