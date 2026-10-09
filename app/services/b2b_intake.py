"""What the shop asks about a business account the moment it registers.

Sign-up no longer asks which price group a firm would like — that was a radio
button on the way to an account, and the answer is a conversation. But a firm
that registers and is never looked at is worse: it trades at retail prices
while nobody knows it is waiting.

So every non-bireysel sign-up opens a pending :class:`B2BApplication` of its
own accord. It carries what the customer already told us — the firm, its tax
details, and the group its line of business suggests — and lands in the admin's
queue, where the documents are checked and the real group is granted.
"""

from app.models.b2b import B2BApplication
from app.models.user import AccountType, InternationalKind, SegmentType
from app.schemas.registration import RegistrationCreate

#: The group a line of business suggests. A kafe serves; a tobacco shop resells,
#: which is a bayi until an admin decides it is a toptancı. Nothing here is
#: granted — the admin approves (or changes) it, and may assign any group.
SUGGESTED_SEGMENT: dict[AccountType, SegmentType] = {
    AccountType.CAFE: SegmentType.CAFE,
    AccountType.TOBACCO_SHOP: SegmentType.DEALER,
    AccountType.SOLE_PROPRIETOR: SegmentType.DEALER,
    AccountType.COMPANY: SegmentType.DEALER,
}

#: An international customer said what it buys for, so it is not a guess at all.
SUGGESTED_FOR_INTERNATIONAL: dict[InternationalKind, SegmentType] = {
    InternationalKind.WHOLESALER: SegmentType.WHOLESALE,
    InternationalKind.CAFE: SegmentType.CAFE,
}


def suggested_segment(payload: RegistrationCreate) -> SegmentType | None:
    """The group to put in the application, or None for a bireysel sign-up."""
    if payload.account_type is AccountType.INTERNATIONAL:
        kind = payload.international_kind
        return SUGGESTED_FOR_INTERNATIONAL.get(InternationalKind(kind)) if kind else SegmentType.WHOLESALE
    return SUGGESTED_SEGMENT.get(AccountType(payload.account_type))


def application_for(user_id, payload: RegistrationCreate) -> B2BApplication | None:
    """The pending application a business sign-up opens for itself.

    None for a bireysel account: it is already on the price list it belongs to.
    """
    segment = suggested_segment(payload)
    if segment is None or payload.company is None:
        return None
    return B2BApplication(
        user_id=user_id,
        requested_segment=segment,
        company_title=payload.company.title,
        # Optional abroad, where a VAT number is not always issued.
        tax_number=payload.company.tax_number,
        tax_office=payload.company.tax_office,
    )
