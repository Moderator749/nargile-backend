"""Who gets the B2C perks, and nobody else.

Sultan Puan, the ciro (volume) discount and the havale discount are retail
promotions. They exist to move retail baskets, and a B2B account already buys
at a tier price that was never costed with them on top — so a kafe, a shop or
an export customer pays its tier price net, whatever it pays with.

Since 2026-11-25 every perk is one question about the price list
(`retail_perks`): is this account on Perakende? Bireysel or Kurumsal makes no
difference — a firm buying at retail prices gets the cart discounts, free
shipping and Sultan Puan like anyone else. A Kafe, Toptan or Bayi account
does not, which is the point: the tier is the better deal, and it is not meant
to be had twice. An export account (Yurt dışı) gets none either: its orders
are quotes at export rates, not retail sales.
"""

from sqlalchemy import select

from app.models.user import AccountType, CustomerSegment, SegmentType, User

#: Said to a B2B account that asks for a retail perk anyway.
PERKS_REFUSED = (
    "Sultan Puan ve indirimler yalnızca Perakende fiyat grubundaki hesaplara tanımlıdır; "
    "hesabınız kendi fiyat listesinden alışveriş yapar"
)


def retail_perks(user: User, segment: SegmentType | None) -> bool:
    """Whether this account gets the retail promotions: the ciro and havale
    discounts and free shipping — any account on the Perakende price list,
    Bireysel or Kurumsal; never Kafe, Toptan, Bayi or an export account.

    The segment is passed in rather than read off ``user.segment``: this is
    called from paths that have it to hand and from one that must not trigger a
    lazy load under an async session.
    """
    if user.account_type == AccountType.INTERNATIONAL:
        return False
    return (segment or SegmentType.RETAIL) == SegmentType.RETAIL


def loyalty_perks(user: User, segment: SegmentType | None) -> bool:
    """Whether this account has Sultan Puan and Sultan Kumbara: earns points,
    spends them, counts towards the annual bonus, sees its wallet, and can be
    given points by an admin.

    The same rule as the cart discounts (`retail_perks`): the Perakende price
    list, Bireysel or Kurumsal; never Kafe, Toptan, Bayi or an export account.
    Kept as its own name so the points code says what it asks.
    """
    return retail_perks(user, segment)


async def _segment_of(db, user: User) -> SegmentType | None:
    code = await db.scalar(select(CustomerSegment.code).where(CustomerSegment.id == user.segment_id)) if user.segment_id else None
    return SegmentType(code) if code else None


async def perks_for(db, user: User) -> bool:
    """`retail_perks` for an account whose segment is not loaded: its code is
    read by the account's segment id (a loaded `user.segment` may be stale)."""
    return retail_perks(user, await _segment_of(db, user))


async def loyalty_for(db, user: User) -> bool:
    """`loyalty_perks`, the segment read by the account's segment id."""
    return loyalty_perks(user, await _segment_of(db, user))


#: Why an account has no Sultan Puan or Sultan Kumbara.
LOYALTY_OFF_FOR_B2B = (
    "Sultan Puan ve Sultan Kumbara yalnızca Perakende fiyat grubundaki hesaplar içindir; "
    "Kafe, Toptan ve Bayi fiyat gruplarında kapalıdır."
)
