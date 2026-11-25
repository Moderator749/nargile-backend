"""Group access rules ("Grup Erişim Kısıtlamaları"), enforced by the API.

A viewer is a tier: GUEST when not signed in, else their customer group
(RETAIL, CAFE, WHOLESALE, DEALER). An admin is never restricted, so the
panel and its previews always see everything. A tier's rules hide:

- categories, each with everything under it: a product listed under any of
  them (primary category or any other) is hidden, whatever else it is in;
- brands: their products are hidden;
- storefront pages, by path ("/kampanyalar" also covers "/kampanyalar/x").

Hidden products are left out of every list (catalog, search, shelves, price
catalogues) and their own endpoints answer 403 with `RESTRICTED` — a notice,
never their prices. The rules are read once per tier and kept for a minute,
or until an admin changes them (`invalidate`).
"""

import time
from dataclasses import dataclass, field

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access import AccessTarget, AccessTier, TierAccessRule
from app.models.catalog import Category, Product
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services.categories import products_in

RESTRICTED_MESSAGE = "Bu sayfa veya ürün grubu hesap türünüz için görüntülemeye kapalıdır."
RESTRICTED = {"code": "ACCESS_RESTRICTED", "message": RESTRICTED_MESSAGE}
LOCALES = ("tr", "en", "ar")
CACHE_SECONDS = 60

ADMIN_ROLES = {UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.STAFF}


@dataclass(frozen=True)
class Restrictions:
    tier: AccessTier
    category_ids: frozenset[int] = field(default_factory=frozenset)  # with their subcategories
    brand_ids: frozenset[int] = field(default_factory=frozenset)
    routes: frozenset[str] = field(default_factory=frozenset)

    @property
    def hides_products(self) -> bool:
        return bool(self.category_ids or self.brand_ids)

    def route_blocked(self, path: str) -> bool:
        wanted = normalize_route(path)
        return any(wanted == r or wanted.startswith(r.rstrip("/") + "/") for r in self.routes if r)


NONE = Restrictions(tier=AccessTier.GUEST)

_cache: dict[AccessTier, tuple[float, Restrictions]] = {}


def invalidate() -> None:
    """Forget the cached rules: an admin changed them."""
    _cache.clear()


def normalize_route(path: str) -> str:
    """A storefront path as the rules keep it: lower case, no language prefix,
    no query or trailing slash, one leading slash ("/TR/Kampanyalar/?a=1" →
    "/kampanyalar")."""
    path = (path or "").split("?")[0].split("#")[0].strip().lower()
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] in LOCALES:
        parts = parts[1:]
    return "/" + "/".join(parts)


_segment_codes: dict[int, AccessTier] = {}


async def tier_of(db: AsyncSession, user: User | None) -> AccessTier | None:
    """The viewer's tier; None for an admin, who is never restricted. Read
    from the account's segment id every time (a loaded `user.segment` may be
    older than an admin's change of group)."""
    if user is None:
        return AccessTier.GUEST
    if UserRole(user.role) in ADMIN_ROLES:
        return None
    if user.segment_id is None:
        return AccessTier.RETAIL
    if user.segment_id not in _segment_codes:
        for sid, code in (await db.execute(select(CustomerSegment.id, CustomerSegment.code))).all():
            _segment_codes[sid] = AccessTier(SegmentType(code).value)
    return _segment_codes.get(user.segment_id, AccessTier.RETAIL)


async def _subtrees(db: AsyncSession, roots: set[int]) -> frozenset[int]:
    if not roots:
        return frozenset()
    children: dict[int | None, list[int]] = {}
    for cid, parent in (await db.execute(select(Category.id, Category.parent_id))).all():
        children.setdefault(parent, []).append(cid)
    found, todo = set(), list(roots)
    while todo:
        cid = todo.pop()
        if cid in found:
            continue
        found.add(cid)
        todo.extend(children.get(cid, []))
    return frozenset(found)


async def restrictions_for_tier(db: AsyncSession, tier: AccessTier | None) -> Restrictions:
    if tier is None:
        return NONE
    hit = _cache.get(tier)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    rows = (
        await db.execute(select(TierAccessRule.target_type, TierAccessRule.target).where(TierAccessRule.tier == tier.value))
    ).all()
    cats = {int(t) for k, t in rows if k == AccessTarget.CATEGORY.value and t.isdigit()}
    brands = {int(t) for k, t in rows if k == AccessTarget.BRAND.value and t.isdigit()}
    routes = {normalize_route(t) for k, t in rows if k == AccessTarget.PAGE_ROUTE.value}
    found = Restrictions(tier=tier, category_ids=await _subtrees(db, cats), brand_ids=frozenset(brands), routes=frozenset(routes))
    _cache[tier] = (time.monotonic() + CACHE_SECONDS, found)
    return found


async def restrictions_for(db: AsyncSession, user: User | None) -> Restrictions:
    """The viewer's restrictions: by their customer group, GUEST when not signed in."""
    return await restrictions_for_tier(db, await tier_of(db, user))


def visible(r: Restrictions):
    """A condition on Product keeping only what this viewer may see."""
    conds = []
    if r.brand_ids:
        conds.append(or_(Product.brand_id.is_(None), Product.brand_id.notin_(r.brand_ids)))
    if r.category_ids:
        conds.append(Product.id.notin_(products_in(list(r.category_ids))))
    return and_(*conds) if conds else true()


async def hidden_product_ids(db: AsyncSession, r: Restrictions, product_ids) -> set:
    """Which of these products the viewer may not see."""
    ids = list(product_ids)
    if not r.hides_products or not ids:
        return set()
    seen = set((await db.execute(select(Product.id).where(Product.id.in_(ids), visible(r)))).scalars())
    return {i for i in ids if i not in seen}


def refuse() -> None:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=RESTRICTED)


async def ensure_product_visible(db: AsyncSession, r: Restrictions, product_id) -> None:
    if await hidden_product_ids(db, r, [product_id]):
        refuse()


def ensure_category_visible(r: Restrictions, category_id: int) -> None:
    if category_id in r.category_ids:
        refuse()


def ensure_brand_visible(r: Restrictions, brand_id: int) -> None:
    if brand_id in r.brand_ids:
        refuse()
