"""Admin "Raporlar": sales and profitability, and each table as an Excel file.

Every report takes the same filters — a period (`from`/`to`, Turkey dates,
both inclusive; this month by default), a customer group, a payment method, a
category (with its subcategories) and a brand — and reads them the same way
(see app.services.reports).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.brand import Brand
from app.models.catalog import Category
from app.models.order import PaymentMethod
from app.models.user import SegmentType, User
from app.schemas.reports import (
    MissingCostRow,
    PaymentMethodRow,
    ProfitRow,
    SegmentRow,
    SlowMoverRow,
    SummaryRead,
    TierMarginRow,
    TopRow,
    TrendRead,
)
from app.services import reports
from app.services.report_export import Column, workbook
from app.services.reports import SEGMENT_LABELS, SEGMENT_ORDER, TR, ReportFilters, current_rates

router = APIRouter(prefix="/admin/reports", tags=["admin-reports"])

MAX_DAYS = 731
EXPORT_LIMIT = 5000
DEFAULT_THRESHOLD = Decimal("15")
PAYMENT_LABELS = {PaymentMethod.CREDIT_CARD: "Kredi Kartı", PaymentMethod.BANK_TRANSFER: "Havale/EFT"}

SaleMethod = Literal["CREDIT_CARD", "BANK_TRANSFER"]
By = Literal["product", "category", "brand"]
TopSort = Literal["quantity", "revenue"]
ProfitBy = Literal["product", "category"]
ProfitSort = Literal["profit", "margin", "revenue"]
Threshold = Annotated[Decimal, Query(ge=0, le=100)]
Limit = Annotated[int, Query(ge=1, le=500)]


def report_filters(
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    segment: SegmentType | None = None,
    payment_method: SaleMethod | None = None,
    category_id: int | None = None,
    brand_id: int | None = None,
) -> ReportFilters:
    date_to = date_to or datetime.now(TR).date()
    date_from = date_from or date_to.replace(day=1)
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="Başlangıç tarihi bitiş tarihinden sonra olamaz")
    if (date_to - date_from).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=422, detail="En fazla iki yıllık bir dönem seçilebilir")
    return ReportFilters(
        date_from=date_from,
        date_to=date_to,
        segment=segment,
        payment_method=PaymentMethod(payment_method) if payment_method else None,
        category_id=category_id,
        brand_id=brand_id,
    )


Filters = Annotated[ReportFilters, Depends(report_filters)]
Admin = Annotated[User, Depends(get_current_admin_user)]
Db = Annotated[AsyncSession, Depends(get_db)]


@router.get("/summary", response_model=SummaryRead)
async def summary(f: Filters, _admin: Admin, db: Db):
    return await reports.summary(db, f, await current_rates())


@router.get("/trend", response_model=TrendRead)
async def trend(f: Filters, _admin: Admin, db: Db):
    return await reports.trend(db, f, await current_rates())


@router.get("/segments", response_model=list[SegmentRow])
async def segments(f: Filters, _admin: Admin, db: Db):
    return await reports.segments(db, f, await current_rates())


@router.get("/top", response_model=list[TopRow])
async def top(f: Filters, _admin: Admin, db: Db, by: By = "product", sort: TopSort = "revenue", limit: Limit = 20):
    return await reports.top(db, f, await current_rates(), by=by, sort=sort, limit=limit)


@router.get("/slow-movers", response_model=list[SlowMoverRow])
async def slow_movers(
    f: Filters, _admin: Admin, db: Db, days: Annotated[int, Query(ge=7, le=365)] = 60, limit: Limit = 100
):
    return await reports.slow_movers(db, f, await current_rates(), days=days, now=datetime.now(TR), limit=limit)


@router.get("/payment-methods", response_model=list[PaymentMethodRow])
async def payment_methods(f: Filters, _admin: Admin, db: Db):
    return await reports.payment_methods(db, f)


@router.get("/profitability", response_model=list[ProfitRow])
async def profitability(
    f: Filters,
    _admin: Admin,
    db: Db,
    by: ProfitBy = "product",
    sort: ProfitSort = "profit",
    threshold: Threshold = DEFAULT_THRESHOLD,
    limit: Limit = 100,
):
    return await reports.profitability(
        db, f, await current_rates(), by=by, sort=sort, threshold=threshold, limit=limit
    )


@router.get("/tier-margins", response_model=list[TierMarginRow])
async def tier_margins(
    f: Filters,
    _admin: Admin,
    db: Db,
    threshold: Threshold = DEFAULT_THRESHOLD,
    only_alerts: bool = False,
    limit: Limit = 200,
):
    return await reports.tier_margins(
        db, f, await current_rates(), threshold=threshold, only_alerts=only_alerts, limit=limit
    )


@router.get("/missing-costs", response_model=list[MissingCostRow])
async def missing_costs(f: Filters, _admin: Admin, db: Db, limit: Limit = 200):
    return await reports.missing_costs(db, f, await current_rates(), now=datetime.now(TR), limit=limit)


# ── Excel'e Aktar ────────────────────────────────────────────────────────────

Report = Literal[
    "trend", "segments", "top", "slow-movers", "payment-methods", "profitability", "tier-margins", "missing-costs"
]
BY_LABELS = {"product": "Ürün", "category": "Kategori", "brand": "Marka"}
TOP_TITLES = {
    "product": "En Çok Satan Ürünler",
    "category": "En Çok Satan Kategoriler",
    "brand": "En Çok Satan Markalar",
}


# Reports about the catalog as it is today: the period, the customer group and
# the payment method do not reach them, so their files do not claim them.
AS_OF_TODAY = {"slow-movers", "tier-margins", "missing-costs"}


async def _describe(db: AsyncSession, f: ReportFilters, *, as_of_today: bool = False) -> str:
    """The period and the filters that shaped the table, for the line under the title."""
    if as_of_today:
        parts = [f"{datetime.now(TR):%d.%m.%Y} itibarıyla"]
    else:
        parts = [f"Dönem: {f.date_from:%d.%m.%Y} – {f.date_to:%d.%m.%Y}"]
        if f.segment:
            parts.append(f"Müşteri grubu: {SEGMENT_LABELS[f.segment]}")
        if f.payment_method:
            parts.append(f"Ödeme: {PAYMENT_LABELS[f.payment_method]}")
    if f.category_id is not None:
        parts.append(f"Kategori: {await db.scalar(select(Category.name).where(Category.id == f.category_id)) or f.category_id}")
    if f.brand_id is not None:
        parts.append(f"Marka: {await db.scalar(select(Brand.name).where(Brand.id == f.brand_id)) or f.brand_id}")
    return " · ".join(parts)


def _top_columns(by: str) -> list[Column]:
    columns = [Column(BY_LABELS[by], lambda r: r.name)]
    if by == "product":
        columns.insert(0, Column("Ürün Kodu", lambda r: r.code))
    return columns + [
        Column("Adet", lambda r: r.quantity, "int"),
        Column("Ciro (KDV dahil)", lambda r: r.revenue, "money"),
        Column("Sipariş", lambda r: r.orders, "int"),
    ]


def _profit_columns(by: str) -> list[Column]:
    columns = [Column(BY_LABELS[by], lambda r: r.name)]
    if by == "product":
        columns.insert(0, Column("Ürün Kodu", lambda r: r.code))
    return columns + [
        Column("Adet", lambda r: r.quantity, "int"),
        Column("Ciro (KDV hariç)", lambda r: r.revenue_excl_vat, "money"),
        Column("Maliyet", lambda r: r.cost, "money"),
        Column("Brüt Kâr", lambda r: r.gross_profit, "money"),
        Column("Marj", lambda r: r.margin_percent, "percent"),
        Column("Maliyetsiz Adet", lambda r: r.uncosted_quantity, "int"),
        Column("Tahmini Maliyet", lambda r: r.estimated, "bool"),
        Column("Eşik Altı", lambda r: r.below_threshold, "bool"),
    ]


def _tier_columns() -> list[Column]:
    columns = [
        Column("Ürün Kodu", lambda r: r.code),
        Column("Ürün", lambda r: r.name),
        Column("Kategori", lambda r: r.category),
        Column("Marka", lambda r: r.brand),
        Column("Maliyet (KDV hariç)", lambda r: r.cost, "money"),
    ]
    for i, segment in enumerate(SEGMENT_ORDER):
        label = SEGMENT_LABELS[segment]
        columns += [
            Column(f"{label} Fiyatı (KDV hariç)", lambda r, i=i: r.tiers[i].price_excl_vat, "money"),
            Column(f"{label} Marjı", lambda r, i=i: r.tiers[i].margin_percent, "percent"),
            Column(f"{label} Havale ile Marj", lambda r, i=i: r.tiers[i].havale_margin_percent, "percent"),
        ]
    return columns + [
        Column("En Düşük Marj", lambda r: r.lowest_margin_percent, "percent"),
        Column("Marj Erozyonu", lambda r: r.alert, "bool"),
    ]


SLOW_COLUMNS = [
    Column("Ürün Kodu", lambda r: r.code),
    Column("Ürün", lambda r: r.name),
    Column("Kategori", lambda r: r.category),
    Column("Marka", lambda r: r.brand),
    Column("Yayında", lambda r: r.is_published, "bool"),
    Column("Stok", lambda r: r.stock, "int"),
    Column("Stok Değeri (maliyet)", lambda r: r.stock_value, "money"),
    Column("Son Satış", lambda r: r.last_sold_at, "datetime"),
]
MISSING_COLUMNS = [
    Column("Ürün Kodu", lambda r: r.code),
    Column("Ürün", lambda r: r.name),
    Column("Kategori", lambda r: r.category),
    Column("Marka", lambda r: r.brand),
    Column("Yayında", lambda r: r.is_published, "bool"),
    Column("Stok", lambda r: r.stock, "int"),
    Column("Son 90 Gün Adet", lambda r: r.sold_quantity_90d, "int"),
    Column("Son 90 Gün Ciro", lambda r: r.revenue_90d, "money"),
]
PAYMENT_COLUMNS = [
    Column("Ödeme Yöntemi", lambda r: PAYMENT_LABELS[r.payment_method]),
    Column("Verilen Sipariş", lambda r: r.placed, "int"),
    Column("Ödenen", lambda r: r.paid, "int"),
    Column("Bekleyen", lambda r: r.pending, "int"),
    Column("İptal", lambda r: r.cancelled, "int"),
    Column("Başarısız Ödeme", lambda r: r.failed, "int"),
    Column("İade", lambda r: r.returned, "int"),
    Column("Ciro (KDV dahil)", lambda r: r.revenue, "money"),
    Column("Dönüşüm", lambda r: r.conversion_percent, "percent"),
    Column("İptal Oranı", lambda r: r.cancellation_percent, "percent"),
]
SEGMENT_COLUMNS = [
    Column("Müşteri Grubu", lambda r: r.label),
    Column("Ciro (KDV dahil)", lambda r: r.revenue, "money"),
    Column("Sipariş", lambda r: r.orders, "int"),
    Column("Ciro Payı", lambda r: r.share_percent, "percent"),
    Column("Brüt Kâr", lambda r: r.gross_profit, "money"),
    Column("Marj", lambda r: r.margin_percent, "percent"),
]
TREND_COLUMNS = {
    "hour": Column("Saat", lambda r: r.start, "datetime"),
    "day": Column("Gün", lambda r: r.start.date(), "date"),
    "week": Column("Hafta Başı", lambda r: r.start.date(), "date"),
}


@router.get("/export/{report}")
async def export(
    report: Report,
    f: Filters,
    _admin: Admin,
    db: Db,
    by: By = "product",
    sort: Literal["quantity", "revenue", "profit", "margin"] = "revenue",
    days: Annotated[int, Query(ge=7, le=365)] = 60,
    threshold: Threshold = DEFAULT_THRESHOLD,
    only_alerts: bool = False,
):
    rates = await current_rates()
    now = datetime.now(TR)
    if report == "trend":
        data = await reports.trend(db, f, rates)
        title, rows = "Satış Trendi", data.points
        columns = [
            TREND_COLUMNS[data.granularity],
            Column("Ciro (KDV dahil)", lambda r: r.revenue, "money"),
            Column("Sipariş", lambda r: r.orders, "int"),
            Column("Ortalama Sepet", lambda r: r.average_order_value, "money"),
        ]
    elif report == "segments":
        title, columns, rows = "Müşteri Grupları", SEGMENT_COLUMNS, await reports.segments(db, f, rates)
    elif report == "top":
        top_sort = sort if sort in ("quantity", "revenue") else "revenue"
        title = TOP_TITLES[by]
        rows = await reports.top(db, f, rates, by=by, sort=top_sort, limit=EXPORT_LIMIT)
        columns = _top_columns(by)
    elif report == "slow-movers":
        title, columns = f"{days} Gündür Satılmayan Stok", SLOW_COLUMNS
        rows = await reports.slow_movers(db, f, rates, days=days, now=now, limit=EXPORT_LIMIT)
    elif report == "payment-methods":
        title, columns, rows = "Ödeme Yöntemleri", PAYMENT_COLUMNS, await reports.payment_methods(db, f)
    elif report == "profitability":
        profit_by = by if by in ("product", "category") else "product"
        profit_sort = sort if sort in ("profit", "margin", "revenue") else "profit"
        title = f"Kârlılık — {BY_LABELS[profit_by]}"
        rows = await reports.profitability(
            db, f, rates, by=profit_by, sort=profit_sort, threshold=threshold, limit=EXPORT_LIMIT
        )
        columns = _profit_columns(profit_by)
    elif report == "tier-margins":
        title, columns = "Fiyat Grubu Marjları", _tier_columns()
        rows = await reports.tier_margins(
            db, f, rates, threshold=threshold, only_alerts=only_alerts, limit=EXPORT_LIMIT
        )
    else:
        title, columns = "Maliyeti Eksik Ürünler", MISSING_COLUMNS
        rows = await reports.missing_costs(db, f, rates, now=now, limit=EXPORT_LIMIT)

    subtitle = await _describe(db, f, as_of_today=report in AS_OF_TODAY)
    if report in ("profitability", "tier-margins"):
        subtitle += f" · Marj eşiği: %{threshold}"
    if report == "missing-costs":
        subtitle += " · Satışlar: son 90 gün"
    content = workbook(title, subtitle, columns, rows)
    dates = f"{now:%Y%m%d}" if report in AS_OF_TODAY else f"{f.date_from:%Y%m%d}-{f.date_to:%Y%m%d}"
    filename = f"rapor-{report}-{dates}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
