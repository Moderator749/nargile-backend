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

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user, get_optional_user
from app.core.database import get_db
from app.models.access import AccessTarget, AccessTier, CustomerAccessRule, TierAccessRule
from app.models.brand import Brand
from app.models.catalog import Category, Product
from app.models.user import CustomerProfile, CustomerSegment, User
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


# ── Per customer ("Cari bazında") ────────────────────────────────────────────


class RuleCustomer(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    company_title: str | None
    phone: str | None
    segment: str | None
    rule_count: int = 0


class CustomerRulesRead(BaseModel):
    customer: RuleCustomer
    rules: TierRules


def _rules_of(rows) -> TierRules:
    rules = TierRules()
    for rule in rows:
        if rule.target_type == AccessTarget.CATEGORY.value and rule.target.isdigit():
            rules.category_ids.append(int(rule.target))
        elif rule.target_type == AccessTarget.BRAND.value and rule.target.isdigit():
            rules.brand_ids.append(int(rule.target))
        elif rule.target_type == AccessTarget.PAGE_ROUTE.value:
            rules.routes.append(rule.target)
    return rules


async def _customer(db: AsyncSession, user_id: uuid.UUID, count: int = 0) -> RuleCustomer:
    row = (
        await db.execute(
            select(User, CustomerProfile.company_title, CustomerSegment.code)
            .outerjoin(CustomerProfile, CustomerProfile.user_id == User.id)
            .outerjoin(CustomerSegment, CustomerSegment.id == User.segment_id)
            .where(User.id == user_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Müşteri bulunamadı")
    user, title, segment = row
    return RuleCustomer(
        id=user.id, email=user.email, full_name=user.full_name, company_title=title, phone=user.phone,
        segment=segment.value if segment is not None else None, rule_count=count,
    )


@admin.get("/customers", response_model=list[RuleCustomer])
async def customers_with_rules(_admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    """The customers that have rules of their own, most rules first."""
    counts = (
        await db.execute(
            select(CustomerAccessRule.user_id, func.count())
            .group_by(CustomerAccessRule.user_id)
            .order_by(func.count().desc())
        )
    ).all()
    return [await _customer(db, user_id, count) for user_id, count in counts]


async def _customer_rules_read(db: AsyncSession, user_id: uuid.UUID) -> CustomerRulesRead:
    rows = (
        await db.scalars(select(CustomerAccessRule).where(CustomerAccessRule.user_id == user_id).order_by(CustomerAccessRule.id))
    ).all()
    return CustomerRulesRead(customer=await _customer(db, user_id, len(rows)), rules=_rules_of(rows))


@admin.get("/customers/{user_id}", response_model=CustomerRulesRead)
async def customer_rules(
    user_id: uuid.UUID, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    return await _customer_rules_read(db, user_id)


@admin.put("/customers/{user_id}", response_model=CustomerRulesRead)
async def replace_customer_rules(
    user_id: uuid.UUID,
    payload: TierRules,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """One customer's own set, as the editor holds it: hidden from them on top
    of their group's rules. Takes effect on their next request."""
    await _customer(db, user_id)  # 404 for an unknown account
    known_categories = set((await db.scalars(select(Category.id).where(Category.id.in_(payload.category_ids)))).all())
    known_brands = set((await db.scalars(select(Brand.id).where(Brand.id.in_(payload.brand_ids)))).all())
    await db.execute(delete(CustomerAccessRule).where(CustomerAccessRule.user_id == user_id))
    now = datetime.now(timezone.utc)
    targets = (
        [(AccessTarget.CATEGORY, str(i)) for i in dict.fromkeys(payload.category_ids) if i in known_categories]
        + [(AccessTarget.BRAND, str(i)) for i in dict.fromkeys(payload.brand_ids) if i in known_brands]
        + [(AccessTarget.PAGE_ROUTE, r) for r in payload.routes]
    )
    for kind, target in targets:
        db.add(CustomerAccessRule(user_id=user_id, target_type=kind.value, target=target, created_at=now, created_by=admin_user.id))
    await db.commit()
    tier_access.invalidate()
    return await _customer_rules_read(db, user_id)
