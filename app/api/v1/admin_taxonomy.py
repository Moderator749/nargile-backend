"""Admin-only menu taxonomy: the two-level category tree, product → category
mapping, and brands (with logos) for the menu and the homepage marquee."""

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_admin_user, get_session_factory
from app.api.v1.brands import brand_read
from app.core.database import get_db
from app.models.brand import Brand
from app.models.catalog import Category, Product, ProductCategory
from app.models.user import User
from app.schemas import i18n
from app.schemas.taxonomy import (
    AdminCategoryRead,
    BrandRead,
    BrandWrite,
    CategoryWrite,
    ProductCategoryAssign,
    ProductCategoryAssignResult,
    slugify,
)
from app.services import slug_redirects
from app.services.categories import memberships, products_in
from app.services.media import MAX_IMAGE_BYTES, InvalidImage, delete_media, save_image
from app.services import auto_translate

router = APIRouter(prefix="/admin", tags=["admin-taxonomy"])


# ── Categories ───────────────────────────────────────────────────────────────


async def _category_rows(db: AsyncSession, ids: list[int] | None = None) -> list[AdminCategoryRead]:
    listed = memberships().subquery()
    counts = (
        select(listed.c.category_id, func.count().label("n"))
        .group_by(listed.c.category_id)
        .subquery()
    )
    stmt = (
        select(Category, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.category_id == Category.id)
        .order_by(Category.sort_order, Category.name, Category.id)
    )
    if ids is not None:
        stmt = stmt.where(Category.id.in_(ids))
    return [
        AdminCategoryRead(
            id=c.id, name=c.name, slug=c.slug, parent_id=c.parent_id, sort_order=c.sort_order,
            is_active=c.is_active, image_url=c.image_url, description=c.description,
            translations=c.translations, product_count=n
        )
        for c, n in (await db.execute(stmt)).all()
    ]


async def _apply_category(db: AsyncSession, category: Category, payload: CategoryWrite) -> None:
    slug = payload.slug or slugify(payload.name)
    if not slug:
        raise HTTPException(status_code=422, detail="Could not derive a slug from the name")
    clash = await db.scalar(select(Category.id).where(Category.slug == slug, Category.id != category.id))
    if clash is not None:
        raise HTTPException(status_code=409, detail=f"Slug '{slug}' is already used")

    if payload.parent_id is not None:
        if payload.parent_id == category.id:
            raise HTTPException(status_code=422, detail="A category cannot be its own parent")
        parent = await db.get(Category, payload.parent_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent category not found")
        # The menu has two levels: parents are top-level, and a parent can't be nested.
        if parent.parent_id is not None:
            raise HTTPException(status_code=422, detail="Subcategories cannot have subcategories")
        if category.id is not None and await db.scalar(select(Category.id).where(Category.parent_id == category.id).limit(1)):
            raise HTTPException(status_code=422, detail="A category with subcategories must stay top-level")

    if category.id is not None and category.slug and category.slug != slug:
        # Renamed: /catalog/<old> keeps leading here.
        await slug_redirects.moved(db, "category", category.slug, slug, category_id=category.id)
    else:
        await slug_redirects.release(db, "category", slug)
    category.name = payload.name
    category.slug = slug
    category.parent_id = payload.parent_id
    category.sort_order = payload.sort_order
    category.is_active = payload.is_active
    category.image_url = payload.image_url
    # Only when sent: a caller that predates the field must not wipe it.
    if "description" in payload.model_fields_set:
        category.description = payload.description
    if "translations" in payload.model_fields_set:
        category.translations = i18n.stored(payload.translations)


@router.get("/categories", response_model=list[AdminCategoryRead])
async def list_categories_admin(
    _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    return await _category_rows(db)


@router.post("/categories", response_model=AdminCategoryRead, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: CategoryWrite,
    background: BackgroundTasks,
    sessions: async_sessionmaker = Depends(get_session_factory),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    category = Category()
    await _apply_category(db, category, payload)
    db.add(category)
    await db.commit()
    background.add_task(auto_translate.translate, "category", category.id, sessions)
    return (await _category_rows(db, [category.id]))[0]


@router.put("/categories/{category_id}", response_model=AdminCategoryRead)
async def update_category(
    category_id: int,
    payload: CategoryWrite,
    background: BackgroundTasks,
    sessions: async_sessionmaker = Depends(get_session_factory),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    await _apply_category(db, category, payload)
    await db.commit()
    background.add_task(auto_translate.translate, "category", category_id, sessions)
    return (await _category_rows(db, [category_id]))[0]


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: int,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    if await db.scalar(select(Category.id).where(Category.parent_id == category_id).limit(1)):
        raise HTTPException(status_code=409, detail="Move or delete its subcategories first")
    if await db.scalar(products_in([category_id]).limit(1)):
        raise HTTPException(status_code=409, detail="Move its products to another category first")
    await db.delete(category)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/products/category", response_model=ProductCategoryAssignResult)
async def assign_product_category(
    payload: ProductCategoryAssign,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-move products into a category (or out of any, with category_id null).

    A move: the category replaces every one the products were listed under.
    """
    if payload.category_id is not None and await db.get(Category, payload.category_id) is None:
        raise HTTPException(status_code=404, detail="Category not found")
    result = await db.execute(
        update(Product)
        .where(Product.id.in_(payload.product_ids))
        .values(category_id=payload.category_id)
        .execution_options(synchronize_session=False)
    )
    await db.execute(delete(ProductCategory).where(ProductCategory.product_id.in_(payload.product_ids)))
    if payload.category_id is not None:
        moved = (await db.execute(select(Product.id).where(Product.id.in_(payload.product_ids)))).scalars()
        db.add_all(ProductCategory(product_id=pid, category_id=payload.category_id, position=0) for pid in moved)
    await db.commit()
    return ProductCategoryAssignResult(updated=result.rowcount)


# ── Brands ───────────────────────────────────────────────────────────────────


async def _brand(db: AsyncSession, brand_id: int) -> Brand:
    brand = await db.get(Brand, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail="Brand not found")
    return brand


async def _apply_brand(db: AsyncSession, brand: Brand, payload: BrandWrite) -> None:
    slug = payload.slug or slugify(payload.name)
    if not slug:
        raise HTTPException(status_code=422, detail="Could not derive a slug from the name")
    clash = await db.scalar(select(Brand.id).where(Brand.slug == slug, Brand.id != brand.id))
    if clash is not None:
        raise HTTPException(status_code=409, detail=f"Slug '{slug}' is already used")
    brand.name = payload.name
    brand.slug = slug
    brand.search_term = (payload.search_term or "").strip() or payload.name
    brand.is_active = payload.is_active
    brand.sort_order = payload.sort_order


@router.get("/brands", response_model=list[BrandRead])
async def list_brands_admin(
    request: Request, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Brand).order_by(Brand.sort_order, Brand.name))
    return [brand_read(b, request) for b in result.scalars()]


@router.post("/brands", response_model=BrandRead, status_code=status.HTTP_201_CREATED)
async def create_brand(
    payload: BrandWrite,
    request: Request,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    brand = Brand()
    await _apply_brand(db, brand, payload)
    db.add(brand)
    await db.commit()
    return brand_read(brand, request)


@router.put("/brands/{brand_id}", response_model=BrandRead)
async def update_brand(
    brand_id: int,
    payload: BrandWrite,
    request: Request,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    brand = await _brand(db, brand_id)
    await _apply_brand(db, brand, payload)
    await db.commit()
    return brand_read(brand, request)


@router.delete("/brands/{brand_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_brand(
    brand_id: int, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    brand = await _brand(db, brand_id)
    logo = brand.logo_path
    await db.delete(brand)
    await db.commit()
    delete_media(logo)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/brands/{brand_id}/logo", response_model=BrandRead)
async def upload_brand_logo(
    brand_id: int,
    request: Request,
    file: UploadFile = File(...),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """PNG, JPEG or WebP up to 1 MB; replaces any previous logo."""
    brand = await _brand(db, brand_id)
    data = await file.read(MAX_IMAGE_BYTES + 1)
    try:
        path = save_image(data, "brands")
    except InvalidImage as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    old, brand.logo_path = brand.logo_path, path
    await db.commit()
    delete_media(old)
    return brand_read(brand, request)


@router.delete("/brands/{brand_id}/logo", response_model=BrandRead)
async def delete_brand_logo(
    brand_id: int,
    request: Request,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    brand = await _brand(db, brand_id)
    old, brand.logo_path = brand.logo_path, None
    await db.commit()
    delete_media(old)
    return brand_read(brand, request)
