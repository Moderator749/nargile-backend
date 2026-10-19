"""Where a B2B order may be delivered.

A firm registers an invoice address and buys against it: its prices, its
documents and its payment terms all hang off that one place. So its orders go
there, and checkout does not offer it a blank address form — what it offers is
the registered address, editable, because a kapı no or a sokak is often written
better the second time.

"Editable" and "anywhere" are different things, and this is the line between
them: the country, the city and the ilçe have to stay the registered ones. The
rest of the address is the customer's to correct for this order. A second branch
in the next ilçe is a new address, which is what this refuses.

There is one way past it, and the customer has to say so: "Şubeme istiyorum".
A firm that names a branch is telling the shop this parcel is for somewhere
else of its own, and that address is then taken as given — in Turkey, and under
the branch's name, which is what the order records.

A bireysel account is not restricted at all: it ships wherever it likes.
"""


from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.countries import TURKEY
from app.models.user import AccountType, CustomerAddress, User
from app.schemas.address import ISTANBUL, ShippingAddress, place_key

#: Said when a B2B order names somewhere its account has never registered.
ELSEWHERE = (
    "Siparişiniz kayıtlı fatura adresinizin bulunduğu il ve ilçeye teslim edilir; "
    "adres ayrıntılarını bu sipariş için düzenleyebilirsiniz"
)
ELSEWHERE_EN = (
    "Orders are delivered to the city on your registered invoice address; "
    "you can edit the address details for this order"
)


def ships_to_registered_address(account_type: AccountType | str) -> bool:
    """Whether this account is held to its registered address."""
    return AccountType(account_type) != AccountType.INDIVIDUAL


def _place(country: str, city: str, district: str | None) -> tuple[str, str, str]:
    """A place, compared the way people write it: folded, and without an ilçe
    where the country has none."""
    return (
        country.upper(),
        place_key(city),
        place_key(district or "") if country.upper() == TURKEY else "",
    )


async def check_delivery_address(
    db: AsyncSession, user: User, address: ShippingAddress, branch_name: str | None = None
) -> None:
    """Raise ValueError if a B2B order is being sent somewhere else.

    An account with nothing on file — one that predates the current sign-up
    form — is left alone: it has no registered address to be held to, and it
    still has to be able to order.

    `branch_name` is the firm's own branch ("Şubeme istiyorum"), which is the
    one answer that opens this up: a named branch anywhere in Turkey is
    accepted, because the firm has said that is where its goods go. It opens
    nothing abroad — an export shipment is quoted against the address it was
    costed on.
    """
    if not ships_to_registered_address(user.account_type):
        return
    if branch_name and address.country.upper() == TURKEY:
        return
    rows = (
        await db.execute(
            select(CustomerAddress.country, CustomerAddress.city, CustomerAddress.district).where(
                CustomerAddress.user_id == user.id
            )
        )
    ).all()
    if not rows:
        return
    registered = {_place(country, city, district) for country, city, district in rows}
    if _place(address.country, address.city, address.district) not in registered:
        raise ValueError(ELSEWHERE_EN if address.country != TURKEY else ELSEWHERE)


async def delivers_in_istanbul(db: AsyncSession, user: User) -> bool:
    """Whether the shop's own İstanbul run can take this account's order.

    It is offered on the registered address, not on the one typed at checkout:
    the van goes to the firm, and a firm registered in Adana is not in İstanbul
    because it wrote İstanbul in a box.
    """
    rows = await db.execute(
        select(CustomerAddress.city).where(
            CustomerAddress.user_id == user.id, CustomerAddress.country == TURKEY
        )
    )
    return any(place_key(city) == place_key(ISTANBUL) for city in rows.scalars())


