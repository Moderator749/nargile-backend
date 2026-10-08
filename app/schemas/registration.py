"""The sign-up payload: one model for all three kinds of account.

The form a customer fills in depends on who they are — an individual, a sole
proprietorship (şahıs firması) or a company (şirket) — and so do the rules:

* **Bireysel** gives a name, a mobile number and an e-mail; a TC number, a
  billing address are optional, and they may ask for the invoice to be made out
  to a firm instead ("Faturayı firmaya istiyorum") — which then needs the firm's
  own address, because that is where the invoice goes.
* **Kafe & Restorant / Nargile & Tobacco Shop** — a firm, by what it sells —
  gives its invoice identity: whether it is a şahıs firması or a şirket, its
  unvan, the name on its tax plate, vergi dairesi, vergi numarası and an
  address. A şirket is registered under a 10-digit VKN and is not a person, so
  no TC number belongs to it; a şahıs firması is a person, may be registered
  under their own TC number, and may give it. Both name the person to call, and
  both must give an address: an invoice needs somewhere to go.
* **Yurt dışı / International** says what it buys for — a wholesaler or a
  kafe, both businesses — gives a company name, a contact and an international
  address, and is held to none of the Turkish rules: no TC checksum, no VKN
  length, no province from the list of 81, and a phone number in whatever shape
  its country writes them. Its tax or VAT number is optional and free-form,
  because there are as many formats as there are jurisdictions. Messages for
  this branch are in English, like its form.

What this payload carries nothing about is pricing. Every self-service sign-up
trades at RETAIL; a Kafe/Toptan/Bayi tier is an admin's to grant, from the
approval queue a customer joins by applying from their account page once they
are in (``POST /me/b2b-application``). Which tier a firm should have is a
conversation, not a radio button on the way to an account.

Nor does it carry anything about delivery. Where an order goes, who signs for
it and which carrier takes it are checkout's questions: they are asked once
there is an order to route, and answered on the order itself. Asking them at
sign-up put a page of decisions between a customer and their account.

A payload that still sends a ``delivery`` block, a ``requested_segment`` or a
``b2b_application`` is accepted, and those are ignored.
"""

import re
from typing import Annotated, Any, Union

from pydantic import BaseModel, Discriminator, Field, Tag, field_validator, model_validator

from app.core.countries import TURKEY
from app.core.tr_identity import normalize_tax_id, normalize_tckn, normalize_vkn
from app.models.user import (
    SELECTABLE_ACCOUNT_TYPES,
    TURKISH_BUSINESS_TYPES,
    AccountType,
    BillingEntity,
    InternationalKind,
)
from app.schemas.address import (
    InternationalAddress,
    StructuredAddress,
    valid_international_phone,
    valid_phone,
)
# Not a full RFC check — the point is to catch a typo before the account is
# created, not to prove the address exists.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

def address_shape(value: Any) -> str:
    """Which of the two shapes an address was sent in, read off its country.

    A tag rather than a plain union, so a payload is checked against one shape
    and gets that shape's complaints: told to pick a real country, say, instead
    of being told its mahalle is missing.
    """
    country = value.get("country") if isinstance(value, dict) else getattr(value, "country", None)
    return "tr" if not country or str(country).upper() == TURKEY else "international"


# One field, either shape — the account type still has to agree with which arrived.
AnyAddress = Annotated[
    Union[
        Annotated[StructuredAddress, Tag("tr")],
        Annotated[InternationalAddress, Tag("international")],
    ],
    Discriminator(address_shape),
]


class CompanyIdentity(BaseModel):
    """Whoever the invoice is made out to.

    The same block serves a business account (where it is the account holder)
    and an individual who wants a corporate invoice (where it is somebody
    else's firm). Either way ``entity_type`` is the customer's answer: whether
    a firm is a şahıs firması or a şirket decides the invoice, the tax number's
    length and whether a TC number belongs with it, and no card implies it.
    """

    entity_type: BillingEntity | None = None
    title: str = Field(min_length=2, max_length=255)  # Unvan
    # Where the firm is — required when the firm is somebody else's, because an
    # invoice has to be addressed to it. A business account sends nothing here:
    # the firm is the account holder, so its address is ``billing_address``.
    address: StructuredAddress | None = None
    # The name on the tax plate — the owner's for a sole proprietorship, the
    # company's for a company.
    tax_plate_name: str | None = Field(default=None, max_length=150)
    tax_office: str | None = Field(default=None, max_length=100)  # Vergi dairesi
    tax_number: str | None = Field(default=None, max_length=30)

    # Only the title is required of every kind of firm. Which of the rest are
    # mandatory, and what shape a tax number has to be, is settled by
    # :class:`RegistrationCreate` — it is the one that knows whose firm this is.

    @field_validator("title")
    @classmethod
    def _collapse(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("tax_plate_name", "tax_office", "tax_number", mode="before")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return " ".join((value or "").split()) or None


class RegistrationCreate(BaseModel):
    """What ``POST /auth/register`` accepts. See the module docstring.

    Fields that belong to another account type are refused rather than ignored:
    a payload that mixes a TC number into a company registration is a bug in the
    client, and silently dropping it would store half of what the customer saw
    on their screen.
    """

    account_type: AccountType = AccountType.INDIVIDUAL
    email: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    # Kullanıcı adı: optional, and a second way to log in once chosen.
    username: str | None = Field(default=None, min_length=3, max_length=30, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,28}[A-Za-z0-9]$")
    full_name: str | None = Field(default=None, max_length=150)  # İsim soyisim (bireysel)
    contact_name: str | None = Field(default=None, max_length=150)  # Yetkili kişi (firma)
    phone: str | None = Field(default=None, max_length=30)  # Cep tel
    landline_phone: str | None = Field(default=None, max_length=30)  # Sabit tel
    # TC kimlik no: an individual's, or the owner's of a şahıs firması.
    national_id: str | None = Field(default=None, max_length=20)
    # Wholesaler or kafe, asked of international customers only — their account
    # type says where they are, not what they buy for.
    international_kind: InternationalKind | None = None
    company: CompanyIdentity | None = None
    billing_address: AnyAddress | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        found = value.strip().lower()
        if not _EMAIL.match(found):
            raise ValueError("Geçerli bir e-posta adresi girilmeli")
        return found

    @field_validator("full_name", "contact_name", "username", mode="before")
    @classmethod
    def _collapse(cls, value: str | None) -> str | None:
        return " ".join((value or "").split()) or None

    @field_validator("account_type")
    @classmethod
    def _offered(cls, account_type: AccountType) -> AccountType:
        """Only what the form offers. The legacy pair exists for rows written
        before the cards changed, not for new sign-ups."""
        if account_type not in SELECTABLE_ACCOUNT_TYPES:
            raise ValueError("Geçersiz hesap türü")
        return account_type

    @model_validator(mode="after")
    def _by_account_type(self) -> "RegistrationCreate":
        international = self.account_type is AccountType.INTERNATIONAL
        check_phone = valid_international_phone if international else valid_phone
        self.phone = check_phone(self.phone or "")
        if self.landline_phone:
            self.landline_phone = check_phone(self.landline_phone)

        if international:
            self._as_international()
        elif self.account_type in TURKISH_BUSINESS_TYPES:
            self._as_business()
        else:
            self._as_individual()

        # The address shape and the account type have to agree: a Turkish
        # account cannot be invoiced abroad, and an international one has no
        # mahalle.
        if self.billing_address is not None:
            foreign = isinstance(self.billing_address, InternationalAddress)
            if international and not foreign:
                raise ValueError("An international account needs an international address")
            if not international and foreign:
                raise ValueError("Yurt içi hesapta Türkiye adresi girilmeli")

        return self

    def _as_individual(self) -> None:
        if self.international_kind is not None:
            raise ValueError("international_kind yalnızca yurt dışı hesaplarda gönderilir")
        if not self.full_name or len(self.full_name) < 2:
            raise ValueError("İsim soyisim girilmeli")
        if self.national_id:
            self.national_id = normalize_tckn(self.national_id)
        if self.company is not None:
            # "Faturayı firmaya istiyorum": they have to say whose kind of firm,
            # and give it in full — it is going on an invoice, which needs an
            # address of its own.
            if self.company.entity_type is None:
                raise ValueError("Fatura için şahıs firması / şirket seçilmeli")
            if self.company.address is None:
                raise ValueError("Kurumsal fatura adresi girilmeli")
            self._require_turkish_tax_identity(self.company.entity_type)
        # Yetkili kişi is a firm's field; an individual is their own contact.
        self.contact_name = None

    def _as_business(self) -> None:
        if self.international_kind is not None:
            raise ValueError("international_kind yalnızca yurt dışı hesaplarda gönderilir")
        if self.company is None:
            raise ValueError("Fatura bilgileri (unvan, vergi dairesi, vergi no) girilmeli")
        # Şahıs firması or şirket: the customer's own answer now, not something
        # read off which card they picked.
        if self.company.entity_type is None:
            raise ValueError("Şahıs firması / şirket seçilmeli")
        if not self.contact_name or len(self.contact_name) < 2:
            raise ValueError("Yetkili kişinin adı soyadı girilmeli")
        if self.billing_address is None:
            raise ValueError("Fatura adresi girilmeli")
        if self.company.address is not None:
            # Its own address is its firm's: there is only one of them.
            raise ValueError("Firma hesabının adresi fatura adresidir")
        if self.national_id:
            # A şahıs firması is a person and may give theirs; a şirket is not.
            if self.company.entity_type is BillingEntity.COMPANY:
                raise ValueError("Şirket hesabında TC kimlik numarası alanı yoktur")
            self.national_id = normalize_tckn(self.national_id)
        self._require_turkish_tax_identity(self.company.entity_type)
        # Admin screens show a person's name; for a firm that is its contact.
        self.full_name = self.contact_name

    def _require_turkish_tax_identity(self, entity: BillingEntity) -> None:
        """A firm registered in Turkey, as its vergi levhası has it: the name on
        the plate, the vergi dairesi, and a number of the right length —
        strictly a VKN for a company, which has no TC number to be registered
        under. The same of whoever is being invoiced, whether that is the
        account holder or a firm they asked us to invoice instead."""
        company = self.company
        assert company is not None
        if not company.tax_plate_name:
            raise ValueError("Vergi levhasındaki isim soyisim girilmeli")
        if not company.tax_office:
            raise ValueError("Vergi dairesi girilmeli")
        if not company.tax_number:
            raise ValueError("Vergi numarası girilmeli")
        company.tax_number = (
            normalize_vkn(company.tax_number)
            if entity is BillingEntity.COMPANY
            else normalize_tax_id(company.tax_number)
        )
        company.entity_type = entity

    def _as_international(self) -> None:
        """Nothing Turkish applies here — see the module docstring."""
        if self.international_kind is None:
            raise ValueError("Tell us what you buy for: wholesale, or your own cafe")
        if self.national_id:
            raise ValueError("A TC identity number belongs to a Turkish account")
        if self.billing_address is None:
            raise ValueError("An address is required")
        if self.company is None or len(self.company.title) < 2:
            raise ValueError("Company name is required")
        if not self.contact_name or len(self.contact_name) < 2:
            raise ValueError("Contact person name is required")
        if self.company.tax_plate_name or self.company.tax_office:
            raise ValueError("Tax office and tax plate name are Turkish fields")
        if self.company.address is not None:
            raise ValueError("Your company's address is the address above")
        # Both kinds are businesses. There is no international equivalent of
        # "şahıs firması", and no format to check a VAT number against: whatever
        # their jurisdiction issued is what goes on the invoice.
        self.company.entity_type = BillingEntity.COMPANY
        # Admin screens show a person's name; for a firm that is its contact.
        self.full_name = self.contact_name
