"""Raporlar: the sums behind the admin's sales and profitability reports.

Everything about a sale is read from one place, `sold_lines`: the order lines
of paid orders (PAID_STATUSES; never a quote), by the day they were paid, in
Turkey time, narrowed by the report's filters. A line's revenue is its total
less its share of the order's discounts (volume, havale, points — spread over
the lines by their totals), VAT included; its revenue before VAT and its cost
are what profit is worked out from.

A line's cost is the one frozen on it at checkout. Lines sold before that was
kept fall back on the product's cost today — reported as estimated — and lines
with neither are left out of every margin, never counted as free.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import Numeric, and_, case, distinct, exists, func, literal, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brand import Brand
from app.models.catalog import Category, CurrencyType, Inventory, Product, ProductVariant, VariantPrice
from app.models.order import PAID_STATUSES, PENDING_STATUSES, Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import CustomerSegment, SegmentType
from app.schemas.reports import (
    Granularity,
    KpiChanges,
    Kpis,
    MissingCostRow,
    PaymentMethodRow,
    Period,
    ProfitRow,
    SegmentRow,
    SlowMoverRow,
    SummaryRead,
    TierMargin,
    TierMarginRow,
    TopRow,
    TrendPoint,
    TrendRead,
)
from app.services.categories import category_subtree_ids, products_in
from app.services.checkout_engine import CENTRAL_STOCK
from app.services.exchange_rates import RatesUnavailable, get_rates

TR = ZoneInfo("Europe/Istanbul")
CENT = Decimal("0.01")
TENTH = Decimal("0.1")
ZERO = Decimal("0.00")
# A line kept from before VAT rates were frozen, whose price row is gone too.
DEFAULT_VAT = Decimal("20")
SEGMENT_ORDER = (SegmentType.RETAIL, SegmentType.CAFE, SegmentType.WHOLESALE, SegmentType.DEALER)
SEGMENT_LABELS = {
    SegmentType.RETAIL: "Perakende",
    SegmentType.CAFE: "Kafe",
    SegmentType.WHOLESALE: "Toptan",
    SegmentType.DEALER: "Bayi",
}
SALE_METHODS = (PaymentMethod.CREDIT_CARD, PaymentMethod.BANK_TRANSFER)
NO_CATEGORY = "Kategorisiz"
NO_BRAND = "Markasız"


@dataclass(frozen=True)
class ReportFilters:
    date_from: date
    date_to: date  # inclusive
    segment: SegmentType | None = None
    payment_method: PaymentMethod | None = None
    category_id: int | None = None
    brand_id: int | None = None

    @property
    def start(self) -> datetime:
        return datetime.combine(self.date_from, time.min, TR)

    @property
    def end(self) -> datetime:
        return datetime.combine(self.date_to + timedelta(days=1), time.min, TR)

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    def previous(self) -> "ReportFilters":
        """The same number of days, just before."""
        return replace(
            self,
            date_from=self.date_from - timedelta(days=self.days),
            date_to=self.date_from - timedelta(days=1),
        )

    @property
    def period(self) -> Period:
        return Period(date_from=self.date_from, date_to=self.date_to)


# ── Shared pieces ────────────────────────────────────────────────────────────


async def current_rates() -> dict[str, Decimal]:
    """TRY per USD/EUR today, for costs and prices kept in those currencies;
    empty when TCMB is unreachable — such figures are then unknown."""
    try:
        return dict((await get_rates()).try_per_unit)
    except RatesUnavailable:
        return {}


def cost_today(rates: dict[str, Decimal]):
    """A variant's cost before VAT in TRY today; NULL without one, or without
    the rate its currency needs."""
    whens = [(ProductVariant.cost_currency == CurrencyType.TRY, ProductVariant.cost_price)]
    for code in (CurrencyType.USD, CurrencyType.EUR):
        if rates.get(code.value):
            whens.append((
                ProductVariant.cost_currency == code,
                ProductVariant.cost_price * literal(rates[code.value], Numeric(12, 4)),
            ))
    return case(*whens, else_=None)


def _catalog_filters(f: ReportFilters) -> list:
    conditions = []
    if f.category_id is not None:
        conditions.append(Product.id.in_(products_in(category_subtree_ids(f.category_id))))
    if f.brand_id is not None:
        conditions.append(Product.brand_id == f.brand_id)
    return conditions


def sold_lines(f: ReportFilters, rates: dict[str, Decimal]):
    """Every line sold in the period that the filters let through."""
    kept = case(
        (Order.total_amount > 0, 1 - Order.discount_amount / Order.total_amount), else_=literal(Decimal(1))
    )
    revenue = OrderItem.line_total * kept
    vat = func.coalesce(OrderItem.vat_rate, DEFAULT_VAT)
    now_cost = cost_today(rates)
    unit_cost = func.coalesce(OrderItem.unit_cost, now_cost)
    q = (
        select(
            OrderItem.order_id.label("order_id"),
            Order.paid_at.label("paid_at"),
            func.coalesce(Order.segment_code, SegmentType.RETAIL).label("segment"),
            Order.recovered_at.label("recovered_at"),
            Product.id.label("product_id"),
            Product.base_code.label("code"),
            Product.name.label("name"),
            Product.category_id.label("category_id"),
            Product.brand_id.label("brand_id"),
            OrderItem.quantity.label("quantity"),
            revenue.label("revenue"),
            (revenue / (1 + vat / 100)).label("revenue_excl"),
            (unit_cost * OrderItem.quantity).label("cost"),
            and_(OrderItem.unit_cost.is_(None), now_cost.is_not(None)).label("estimated"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(ProductVariant, ProductVariant.id == OrderItem.variant_id)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(
            Order.status.in_(PAID_STATUSES),
            Order.is_quote.is_(False),
            Order.paid_at >= f.start,
            Order.paid_at < f.end,
            *_catalog_filters(f),
        )
    )
    if f.segment is not None:
        q = q.where(func.coalesce(Order.segment_code, SegmentType.RETAIL) == f.segment)
    if f.payment_method is not None:
        q = q.where(Order.payment_method == f.payment_method)
    return q.subquery("lines")


def _money(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def _percent(part, whole) -> Decimal | None:
    if part is None or not whole:
        return None
    return (Decimal(part) * 100 / Decimal(whole)).quantize(TENTH, rounding=ROUND_HALF_UP)


def _profit(costed_revenue, cost) -> tuple[Decimal | None, Decimal | None]:
    """(gross profit, margin %) on the costed lines; (None, None) without any."""
    if costed_revenue is None or cost is None:
        return None, None
    profit = Decimal(costed_revenue) - Decimal(cost)
    return _money(profit), _percent(profit, costed_revenue)


def _profit_columns(lines):
    """Revenue before VAT on the lines with a cost, and that cost."""
    costed = lines.c.cost.is_not(None)
    return (
        func.sum(lines.c.revenue_excl).filter(costed).label("costed_revenue"),
        func.sum(lines.c.cost).label("cost_total"),
    )


def _change(now: Decimal | int, before: Decimal | int) -> Decimal | None:
    if not before:
        return None
    return ((Decimal(now) - Decimal(before)) * 100 / Decimal(before)).quantize(TENTH, rounding=ROUND_HALF_UP)


# ── Summary ──────────────────────────────────────────────────────────────────


async def kpis(db: AsyncSession, f: ReportFilters, rates: dict[str, Decimal]) -> Kpis:
    lines = sold_lines(f, rates)
    recovered = lines.c.recovered_at.is_not(None)
    row = (
        await db.execute(
            select(
                func.sum(lines.c.revenue),
                func.count(distinct(lines.c.order_id)),
                func.sum(lines.c.revenue_excl),
                *_profit_columns(lines),
                func.count().filter(lines.c.estimated),
                func.sum(lines.c.revenue).filter(recovered),
                func.count(distinct(lines.c.order_id)).filter(recovered),
            )
        )
    ).one()
    revenue, orders, revenue_excl, costed_revenue, cost, estimated, recovered_revenue, recovered_orders = row
    profit, margin = _profit(costed_revenue, cost)
    return Kpis(
        revenue=_money(revenue),
        orders=orders,
        average_order_value=_money(Decimal(revenue) / orders) if orders else ZERO,
        revenue_excl_vat=_money(revenue_excl),
        gross_profit=profit,
        margin_percent=margin,
        costed_share_percent=_percent(costed_revenue or 0, revenue_excl) if revenue_excl else None,
        estimated_cost_lines=estimated,
        recovered_revenue=_money(recovered_revenue),
        recovered_orders=recovered_orders,
    )


async def summary(db: AsyncSession, f: ReportFilters, rates: dict[str, Decimal]) -> SummaryRead:
    before = f.previous()
    now, then = await kpis(db, f, rates), await kpis(db, before, rates)
    margin_points = (
        now.margin_percent - then.margin_percent
        if now.margin_percent is not None and then.margin_percent is not None
        else None
    )
    return SummaryRead(
        period=f.period,
        previous_period=before.period,
        current=now,
        previous=then,
        changes=KpiChanges(
            revenue=_change(now.revenue, then.revenue),
            orders=_change(now.orders, then.orders),
            average_order_value=_change(now.average_order_value, then.average_order_value),
            gross_profit=_change(now.gross_profit or 0, then.gross_profit or 0),
            recovered_revenue=_change(now.recovered_revenue, then.recovered_revenue),
            margin_points=margin_points,
        ),
    )


# ── Trend ────────────────────────────────────────────────────────────────────


def granularity_for(f: ReportFilters) -> Granularity:
    if f.days == 1:
        return "hour"
    return "day" if f.days <= 92 else "week"


async def trend(db: AsyncSession, f: ReportFilters, rates: dict[str, Decimal]) -> TrendRead:
    unit = granularity_for(f)
    lines = sold_lines(f, rates)
    # Literals, not parameters: GROUP BY must see the very expression selected.
    bucket = func.date_trunc(
        literal_column(f"'{unit}'"), func.timezone(literal_column(f"'{TR.key}'"), lines.c.paid_at)
    ).label("bucket")
    rows = (
        await db.execute(
            select(bucket, func.sum(lines.c.revenue), func.count(distinct(lines.c.order_id))).group_by(bucket)
        )
    ).all()
    found = {b.replace(tzinfo=None): (revenue, orders) for b, revenue, orders in rows}

    step = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(weeks=1)}[unit]
    cursor = datetime.combine(f.date_from, time.min)
    if unit == "week":
        cursor -= timedelta(days=cursor.weekday())  # date_trunc's weeks start on Monday
    end = datetime.combine(f.date_to + timedelta(days=1), time.min)
    points = []
    while cursor < end:
        revenue, orders = found.get(cursor, (0, 0))
        points.append(TrendPoint(
            start=cursor.replace(tzinfo=TR),
            revenue=_money(revenue),
            orders=orders,
            average_order_value=_money(Decimal(revenue) / orders) if orders else ZERO,
        ))
        cursor += step
    return TrendRead(granularity=unit, points=points)


# ── Customer groups ──────────────────────────────────────────────────────────


async def segment_labels(db: AsyncSession) -> dict[SegmentType, str]:
    rows = (await db.execute(select(CustomerSegment.code, CustomerSegment.label))).all()
    return {**SEGMENT_LABELS, **{SegmentType(code): label for code, label in rows}}


async def segments(db: AsyncSession, f: ReportFilters, rates: dict[str, Decimal]) -> list[SegmentRow]:
    lines = sold_lines(f, rates)
    rows = (
        await db.execute(
            select(
                lines.c.segment,
                func.sum(lines.c.revenue),
                func.count(distinct(lines.c.order_id)),
                *_profit_columns(lines),
            ).group_by(lines.c.segment)
        )
    ).all()
    by_segment = {SegmentType(r[0]): r[1:] for r in rows}
    total = sum((Decimal(r[0] or 0) for r in by_segment.values()), Decimal(0))
    labels = await segment_labels(db)
    out = []
    for code in SEGMENT_ORDER:
        if f.segment is not None and code != f.segment:
            continue
        revenue, orders, costed_revenue, cost = by_segment.get(code, (0, 0, None, None))
        profit, margin = _profit(costed_revenue, cost)
        out.append(SegmentRow(
            segment=code,
            label=labels[code],
            revenue=_money(revenue),
            orders=orders,
            share_percent=_percent(revenue or 0, total) or Decimal("0.0"),
            gross_profit=profit,
            margin_percent=margin,
        ))
    return out


# ── Best sellers ─────────────────────────────────────────────────────────────


def _grouping(lines, by: str):
    """(key, name, code) columns and the joins they need, for product/category/brand."""
    if by == "product":
        return (lines.c.product_id, lines.c.name, lines.c.code), [], (lines.c.product_id, lines.c.name, lines.c.code)
    if by == "category":
        name = func.coalesce(Category.name, NO_CATEGORY)
        return (
            (lines.c.category_id, name, literal(None)),
            [(Category, Category.id == lines.c.category_id)],
            (lines.c.category_id, Category.name),
        )
    name = func.coalesce(Brand.name, NO_BRAND)
    return (
        (lines.c.brand_id, name, literal(None)),
        [(Brand, Brand.id == lines.c.brand_id)],
        (lines.c.brand_id, Brand.name),
    )


def _select_grouped(lines, by: str, *columns):
    (key, name, code), joins, group = _grouping(lines, by)
    q = select(key, name, code, *columns).select_from(lines)
    for target, on in joins:
        q = q.outerjoin(target, on)
    return q.group_by(*group)


async def top(
    db: AsyncSession, f: ReportFilters, rates: dict[str, Decimal], *, by: str, sort: str, limit: int
) -> list[TopRow]:
    lines = sold_lines(f, rates)
    quantity = func.sum(lines.c.quantity).label("quantity")
    revenue = func.sum(lines.c.revenue).label("revenue")
    q = _select_grouped(lines, by, quantity, revenue, func.count(distinct(lines.c.order_id)))
    order = (quantity.desc(), revenue.desc()) if sort == "quantity" else (revenue.desc(), quantity.desc())
    rows = (await db.execute(q.order_by(*order).limit(limit))).all()
    return [
        TopRow(key="" if key is None else str(key), name=name, code=code, quantity=qty, revenue=_money(rev), orders=n)
        for key, name, code, qty, rev, n in rows
    ]


# ── Profitability ────────────────────────────────────────────────────────────


async def profitability(
    db: AsyncSession,
    f: ReportFilters,
    rates: dict[str, Decimal],
    *,
    by: str,
    sort: str,
    threshold: Decimal,
    limit: int,
) -> list[ProfitRow]:
    lines = sold_lines(f, rates)
    costed_revenue, cost = _profit_columns(lines)
    profit = (costed_revenue - cost).label("profit")
    margin = (profit * 100 / func.nullif(costed_revenue, 0)).label("margin")
    revenue_excl = func.sum(lines.c.revenue_excl).label("revenue_excl")
    q = _select_grouped(
        lines,
        by,
        func.sum(lines.c.quantity),
        revenue_excl,
        costed_revenue,
        cost,
        func.coalesce(func.sum(lines.c.quantity).filter(lines.c.cost.is_(None)), 0),
        func.coalesce(func.bool_or(lines.c.estimated), False),
    )
    order = {
        "profit": (profit.desc().nulls_last(), revenue_excl.desc()),
        "margin": (margin.asc().nulls_last(), revenue_excl.desc()),
        "revenue": (revenue_excl.desc(),),
    }[sort]
    rows = (await db.execute(q.order_by(*order).limit(limit))).all()
    out = []
    for key, name, code, qty, rev_excl, costed, cost_total, uncosted, estimated in rows:
        gross, pct = _profit(costed, cost_total)
        out.append(ProfitRow(
            key="" if key is None else str(key),
            name=name,
            code=code,
            quantity=qty,
            revenue_excl_vat=_money(rev_excl),
            cost=_money(cost_total) if cost_total is not None else None,
            gross_profit=gross,
            margin_percent=pct,
            uncosted_quantity=uncosted,
            estimated=estimated,
            below_threshold=pct is not None and pct < threshold,
        ))
    return out


# ── Payment methods ──────────────────────────────────────────────────────────


async def payment_methods(db: AsyncSession, f: ReportFilters) -> list[PaymentMethodRow]:
    """Card against havale, by the orders placed in the period: how many were
    paid, are still waiting, were cancelled, or failed at the card."""
    status = Order.status
    paid = status.in_(PAID_STATUSES)
    count_if = lambda condition: func.count().filter(condition)  # noqa: E731
    q = select(
        Order.payment_method,
        func.count(),
        count_if(or_(paid, status == OrderStatus.RETURNED)),
        count_if(status.in_(PENDING_STATUSES)),
        count_if(status == OrderStatus.CANCELLED),
        count_if(status == OrderStatus.PAYMENT_FAILED),
        count_if(status == OrderStatus.RETURNED),
        # The goods after discounts, as everywhere in these reports: shipping left out.
        func.sum(Order.total_amount - Order.discount_amount).filter(paid),
    ).where(
        Order.is_quote.is_(False),
        Order.payment_method.in_(SALE_METHODS),
        Order.created_at >= f.start,
        Order.created_at < f.end,
    )
    if f.segment is not None:
        q = q.where(func.coalesce(Order.segment_code, SegmentType.RETAIL) == f.segment)
    catalog = _catalog_filters(f)
    if catalog:
        q = q.where(
            exists(
                select(OrderItem.id)
                .join(ProductVariant, ProductVariant.id == OrderItem.variant_id)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(OrderItem.order_id == Order.id, *catalog)
            )
        )
    rows = {PaymentMethod(r[0]): r[1:] for r in (await db.execute(q.group_by(Order.payment_method))).all()}
    out = []
    for method in SALE_METHODS:
        if f.payment_method is not None and method != f.payment_method:
            continue
        placed, ever_paid, pending, cancelled, failed, returned, revenue = rows.get(method, (0, 0, 0, 0, 0, 0, None))
        settled = placed - pending
        out.append(PaymentMethodRow(
            payment_method=method,
            placed=placed,
            paid=ever_paid,
            pending=pending,
            cancelled=cancelled,
            failed=failed,
            returned=returned,
            revenue=_money(revenue),
            conversion_percent=_percent(ever_paid, settled),
            cancellation_percent=_percent(cancelled + failed, settled),
        ))
    return out


# ── Stock that does not move ─────────────────────────────────────────────────


def _stock(rates: dict[str, Decimal]):
    """Per product: units in the central stock, and their worth at today's cost."""
    on_hand = func.greatest(Inventory.quantity, 0)
    now_cost = cost_today(rates)
    return (
        select(
            ProductVariant.product_id.label("product_id"),
            func.sum(on_hand).label("stock"),
            # NULL as soon as one stocked colour has no cost: a partial worth would mislead.
            func.sum(on_hand * now_cost).label("value"),
            func.count().filter(and_(on_hand > 0, now_cost.is_(None))).label("uncosted"),
        )
        .join(Inventory, and_(Inventory.variant_id == ProductVariant.id, Inventory.warehouse_type == CENTRAL_STOCK))
        .where(ProductVariant.is_active.is_(True))
        .group_by(ProductVariant.product_id)
        .subquery("stock")
    )


def _last_sold():
    return (
        select(ProductVariant.product_id.label("product_id"), func.max(Order.paid_at).label("last_sold_at"))
        .join(OrderItem, OrderItem.variant_id == ProductVariant.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.status.in_(PAID_STATUSES), Order.is_quote.is_(False))
        .group_by(ProductVariant.product_id)
        .subquery("last_sold")
    )


async def slow_movers(
    db: AsyncSession, f: ReportFilters, rates: dict[str, Decimal], *, days: int, now: datetime, limit: int
) -> list[SlowMoverRow]:
    """In stock, and not sold for `days` days — counted from today, not from
    the report's period. A product listed within those days has not had the
    time to prove it, and is left out."""
    cutoff = now - timedelta(days=days)
    stock, last = _stock(rates), _last_sold()
    q = (
        select(
            Product.id, Product.base_code, Product.name, Category.name, Brand.name, Product.is_published,
            stock.c.stock, case((stock.c.uncosted > 0, None), else_=stock.c.value), last.c.last_sold_at,
        )
        .join(stock, stock.c.product_id == Product.id)
        .outerjoin(last, last.c.product_id == Product.id)
        .outerjoin(Category, Category.id == Product.category_id)
        .outerjoin(Brand, Brand.id == Product.brand_id)
        .where(
            stock.c.stock > 0,
            Product.created_at < cutoff,
            or_(last.c.last_sold_at.is_(None), last.c.last_sold_at < cutoff),
            *_catalog_filters(f),
        )
        .order_by(stock.c.value.desc().nulls_last(), stock.c.stock.desc())
        .limit(limit)
    )
    return [
        SlowMoverRow(
            product_id=pid, code=code, name=name, category=category, brand=brand, is_published=published,
            stock=units, stock_value=_money(value) if value is not None else None, last_sold_at=last_sold,
        )
        for pid, code, name, category, brand, published, units, value, last_sold in (await db.execute(q)).all()
    ]


# ── Costs: the gaps, and today's list prices against them ────────────────────


async def missing_costs(
    db: AsyncSession, f: ReportFilters, rates: dict[str, Decimal], *, now: datetime, limit: int
) -> list[MissingCostRow]:
    """Products with a colour on sale that has no cost, those selling most first."""
    uncosted = (
        select(ProductVariant.product_id)
        .where(ProductVariant.is_active.is_(True), ProductVariant.cost_price.is_(None))
        .distinct()
    )
    recent = sold_lines(ReportFilters(date_from=(now - timedelta(days=90)).astimezone(TR).date(), date_to=now.astimezone(TR).date()), rates)
    sold = (
        select(
            recent.c.product_id,
            func.sum(recent.c.quantity).label("quantity"),
            func.sum(recent.c.revenue).label("revenue"),
        )
        .group_by(recent.c.product_id)
        .subquery("sold")
    )
    stock = _stock(rates)
    q = (
        select(
            Product.id, Product.base_code, Product.name, Category.name, Brand.name, Product.is_published,
            func.coalesce(stock.c.stock, 0), func.coalesce(sold.c.quantity, 0), func.coalesce(sold.c.revenue, 0),
        )
        .outerjoin(stock, stock.c.product_id == Product.id)
        .outerjoin(sold, sold.c.product_id == Product.id)
        .outerjoin(Category, Category.id == Product.category_id)
        .outerjoin(Brand, Brand.id == Product.brand_id)
        .where(Product.id.in_(uncosted), *_catalog_filters(f))
        .order_by(func.coalesce(sold.c.revenue, 0).desc(), func.coalesce(stock.c.stock, 0).desc(), Product.name)
        .limit(limit)
    )
    return [
        MissingCostRow(
            product_id=pid, code=code, name=name, category=category, brand=brand, is_published=published,
            stock=units, sold_quantity_90d=qty, revenue_90d=_money(revenue),
        )
        for pid, code, name, category, brand, published, units, qty, revenue in (await db.execute(q)).all()
    ]


def _price_excl_try(rows: list[VariantPrice], rates: dict[str, Decimal]) -> Decimal | None:
    """A group's price before VAT in TRY: its TRY row when it has one (as
    checkout charges), else its foreign row at today's rate."""
    if not rows:
        return None
    row = next((r for r in rows if CurrencyType(r.currency) == CurrencyType.TRY), rows[0])
    currency = CurrencyType(row.currency)
    if currency == CurrencyType.TRY:
        return row.price_excl_tax
    rate = rates.get(currency.value)
    return (row.price_excl_tax * rate).quantize(CENT) if rate else None


async def tier_margins(
    db: AsyncSession,
    f: ReportFilters,
    rates: dict[str, Decimal],
    *,
    threshold: Decimal,
    only_alerts: bool,
    limit: int,
) -> list[TierMarginRow]:
    """Each costed product's list price per customer group against its cost
    today, lowest margin first. Prices are product-wide in practice, so the
    first colour on sale stands for the product."""
    now_cost = cost_today(rates)
    first = (
        select(ProductVariant.id, ProductVariant.product_id, now_cost.label("cost"))
        .where(ProductVariant.is_active.is_(True), ProductVariant.cost_price.is_not(None))
        .distinct(ProductVariant.product_id)
        .order_by(ProductVariant.product_id, ProductVariant.position, ProductVariant.sku)
        .subquery("first_variant")
    )
    products = (
        await db.execute(
            select(first.c.id, first.c.cost, Product.id, Product.base_code, Product.name, Category.name, Brand.name)
            .join(Product, Product.id == first.c.product_id)
            .outerjoin(Category, Category.id == Product.category_id)
            .outerjoin(Brand, Brand.id == Product.brand_id)
            .where(first.c.cost.is_not(None), *_catalog_filters(f))
        )
    ).all()
    if not products:
        return []
    codes = {seg_id: SegmentType(code) for seg_id, code in (await db.execute(select(CustomerSegment.id, CustomerSegment.code))).all()}
    prices: dict = {}
    for row in (
        await db.scalars(select(VariantPrice).where(VariantPrice.variant_id.in_([p[0] for p in products])))
    ).all():
        prices.setdefault((row.variant_id, codes.get(row.segment_id)), []).append(row)

    out = []
    for variant_id, cost, pid, code, name, category, brand in products:
        cost = Decimal(cost)
        tiers = []
        for segment in SEGMENT_ORDER:
            price = _price_excl_try(prices.get((variant_id, segment), []), rates)
            margin = _percent(price - cost, price) if price else None
            tiers.append(TierMargin(
                segment=segment,
                price_excl_vat=price,
                margin_percent=margin,
                below_threshold=margin is not None and margin < threshold,
            ))
        known = [t.margin_percent for t in tiers if t.margin_percent is not None]
        row = TierMarginRow(
            product_id=pid, code=code, name=name, category=category, brand=brand,
            cost=_money(cost), tiers=tiers,
            lowest_margin_percent=min(known) if known else None,
            alert=any(t.below_threshold for t in tiers),
        )
        if row.alert or not only_alerts:
            out.append(row)
    out.sort(key=lambda r: (r.lowest_margin_percent is None, r.lowest_margin_percent or 0, r.name))
    return out[:limit]
