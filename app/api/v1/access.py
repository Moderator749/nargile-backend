"""Group access rules: the storefront's check, and the admin's editor.

- ``GET /access/check?path=…``: may this viewer (signed in or not) open this
  storefront page? A page rule, or the category, brand or product the path
  names (``/<slug>``, ``/marka/<slug>``), decides. The storefront asks it on
  every page change for a signed-in viewer; the data endpoints enforce the
  same rules on their own, so skipping the check shows nothing hidden.
- ``GET /access/guest``: what is hidden from a visitor who is not signed in,
  for the storefront's proxy to answer with the notice.
- ``/admin/access-rules``: every tier's rules, and a tier's set replaced.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user, get_optional_user
from app.core.database import get_db
from app.models.access import AccessTarget, AccessTier, TierAccessRule
from app.models.brand import Brand
from app.models.catalog import Category, Product
from app.models.user import User
from app.services import tier_access
from app.services.tier_access import RESTRICTED_MESSAGE, normalize_route, restrictions_for, restrictions_for_tier

router = APIRouter(prefix="/access", tags=["access"])
admin = APIRouter(prefix="/admin/access-rules", tags=["admin-access"])


class AccessCheck(BaseModel):
    allowed: bool
    message: str | None = None


@router.get("/check", response_model=AccessCheck)
async def check(
    path: str = Query(..., max_length=500, description="A storefront path, with or without its language prefix"),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    access = await restrictions_for(db, user)
    refused = AccessCheck(allowed=False, message=RESTRICTED_MESSAGE)
    route = normalize_route(path)
    if access.route_blocked(route):
        return refused
    parts = [p for p in route.split("/") if p]
    slug = parts[1] if len(parts) == 2 and parts[0] == "marka" else parts[0] if len(parts) == 1 else None
    if slug and (access.category_ids or access.brand_ids):
        category_id = await db.scalar(select(Category.id).where(Category.slug == slug))
        if category_id is not None and category_id in access.category_ids:
            return refused
        brand_id = await db.scalar(select(Brand.id).where(Brand.slug == slug))
        if brand_id is not None and brand_id in access.brand_ids:
            return refused
        product_id = await db.scalar(select(Product.id).where(Product.slug == slug, Product.is_deleted.is_(False)))
        if product_id is not None and await tier_access.hidden_product_ids(db, access, [product_id]):
            return refused
    return AccessCheck(allowed=True)


class GuestAccess(BaseModel):
    routes: list[str]
    # Whether any category or brand is hidden from guests: only then must a
    # product, category or brand address be checked one by one.
    catalog: bool


@router.get("/guest", response_model=GuestAccess)
async def guest(db: AsyncSession = Depends(get_db)):
    """What a visitor who is not signed in may not see, for the storefront's
    proxy: the closed pages, and whether any catalog rule exists at all."""
    r = await restrictions_for_tier(db, AccessTier.GUEST)
    return GuestAccess(routes=sorted(r.routes), catalog=r.hides_products)


# ── Admin ────────────────────────────────────────────────────────────────────


class TierRules(BaseModel):
    category_ids: list[int] = Field(default_factory=list, max_length=2000)
    brand_ids: list[int] = Field(default_factory=list, max_length=2000)
    routes: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("routes")
    @classmethod
    def _paths(cls, v: list[str]) -> list[str]:
        out = []
        for raw in v:
            path = normalize_route(raw)
            if path == "/":
                raise ValueError("Ana sayfa kapatılamaz; bir sayfa yolu girin (ör. /firsat-urunleri)")
            if len(path) > 255:
                raise ValueError("Sayfa yolu çok uzun")
            if path not in out:
                out.append(path)
        return out


class Option(BaseModel):
    id: int
    name: str
    parent_id: int | None = None
    is_active: bool = True


class AccessRulesRead(BaseModel):
    tiers: dict[AccessTier, TierRules]
    categories: list[Option]
    brands: list[Option]


async def _read(db: AsyncSession) -> AccessRulesRead:
    tiers = {tier: TierRules() for tier in AccessTier}
    for rule in (await db.scalars(select(TierAccessRule).order_by(TierAccessRule.id))).all():
        rules = tiers.setdefault(AccessTier(rule.tier), TierRules())
        if rule.target_type == AccessTarget.CATEGORY.value and rule.target.isdigit():
            rules.category_ids.append(int(rule.target))
        elif rule.target_type == AccessTarget.BRAND.value and rule.target.isdigit():
            rules.brand_ids.append(int(rule.target))
        elif rule.target_type == AccessTarget.PAGE_ROUTE.value:
            rules.routes.append(rule.target)
    categories = (await db.scalars(select(Category).order_by(Category.sort_order, Category.name))).all()
    brands = (await db.scalars(select(Brand).order_by(Brand.sort_order, Brand.name))).all()
    return AccessRulesRead(
        tiers=tiers,
        categories=[Option(id=c.id, name=c.name, parent_id=c.parent_id, is_active=c.is_active) for c in categories],
        brands=[Option(id=b.id, name=b.name, is_active=b.is_active) for b in brands],
    )


@admin.get("", response_model=AccessRulesRead)
async def list_rules(_admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    return await _read(db)


@admin.put("/{tier}", response_model=AccessRulesRead)
async def replace_rules(
    tier: AccessTier,
    payload: TierRules,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """A tier's whole set, as the editor holds it: what is listed is hidden
    from that tier, what is not is shown. Takes effect on the next request."""
    known_categories = set((await db.scalars(select(Category.id).where(Category.id.in_(payload.category_ids)))).all())
    known_brands = set((await db.scalars(select(Brand.id).where(Brand.id.in_(payload.brand_ids)))).all())
    await db.execute(delete(TierAccessRule).where(TierAccessRule.tier == tier.value))
    now = datetime.now(timezone.utc)
    targets = (
        [(AccessTarget.CATEGORY, str(i)) for i in dict.fromkeys(payload.category_ids) if i in known_categories]
        + [(AccessTarget.BRAND, str(i)) for i in dict.fromkeys(payload.brand_ids) if i in known_brands]
        + [(AccessTarget.PAGE_ROUTE, r) for r in payload.routes]
    )
    for kind, target in targets:
        db.add(TierAccessRule(tier=tier.value, target_type=kind.value, target=target, created_at=now, created_by=admin_user.id))
    await db.commit()
    tier_access.invalidate()
    return await _read(db)
