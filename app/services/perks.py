"""Who gets the B2C perks, and nobody else.

ParaPuan, the ciro (volume) discount and the havale discount are retail
promotions. They exist to move retail baskets, and a B2B account already buys
at a tier price that was never costed with them on top — so a kafe, a shop or
an export customer pays its tier price net, whatever it pays with.

That makes eligibility one question, asked in one place: is this a bireysel
account on the retail price list? A bireysel account an admin has moved to a
B2B tier is not, which is the point — the tier is the better deal, and it is
not meant to be had twice.
"""

from app.models.user import AccountType, SegmentType, User

#: Said to a B2B account that asks for a retail perk anyway.
PERKS_REFUSED = (
    "ParaPuan ve indirimler yalnızca bireysel (perakende) hesaplara tanımlıdır; "
    "hesabınız kendi fiyat listesinden alışveriş yapar"
)


def retail_perks(user: User, segment: SegmentType | None) -> bool:
    """Whether this account earns points, spends them, and gets cart discounts.

    The segment is passed in rather than read off ``user.segment``: this is
    called from paths that have it to hand and from one that must not trigger a
    lazy load under an async session.
    """
    if user.account_type != AccountType.INDIVIDUAL:
        return False
    return (segment or SegmentType.RETAIL) == SegmentType.RETAIL
