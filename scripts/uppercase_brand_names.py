"""Brand names in capitals, once: "Shisha Foil" → "SHISHA FOIL", "telamon bowl"
→ "TELAMON BOWL" (app.core.brand_names: "i" → "I", never "İ").

From now on the panel enters brand names in capitals and the storefront shows
them as stored; this brings the names entered before into line.

- `brands.name`: every name with a small letter, in capitals.
- Two or more brands that come to the same name ("Shisha Foil" and "SHISHA
  FOIL") become one: the one with a logo (then the active one, the one with a
  description, the one already in capitals, the first in the menu, the oldest)
  keeps its row and slug, takes the others' products, and their logo and
  description where it has none; the others are deleted. Their slugs are
  listed: their pages are gone.
- `products.brand`, the brand's name kept on the product: in capitals too
  (a product linked to a brand carries that brand's new name).

Idempotent: a second run finds nothing to do. Reads first and writes last, in
one transaction, with a short lock timeout on the hot `products` table.

    venv/bin/python scripts/uppercase_brand_names.py            # show, write nothing
    venv/bin/python scripts/uppercase_brand_names.py --apply    # write (pg_dump first)

Every --apply run writes the rows it changes, as they were, to
scripts/migration_reports/brand-caps-<date>.json. The storefront's cache is
not dropped from here: POST its /api/revalidate afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import bindparam, delete, select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.brand_names import brand_caps  # noqa: E402
from app.models.brand import Brand  # noqa: E402
from app.models.catalog import Product  # noqa: E402

REPORTS = Path(__file__).resolve().parent / "migration_reports"


@dataclass
class Plan:
    renamed: list[dict] = field(default_factory=list)  # {id, slug, before, after}
    merged: list[dict] = field(default_factory=list)  # {name, keep: {id, slug}, drop: [{id, slug, name, products}]}
    products: list[dict] = field(default_factory=list)  # {id, before, after, brand_id_before, brand_id_after}
    brands_before: list[dict] = field(default_factory=list)  # every touched brand row, as it was

    @property
    def empty(self) -> bool:
        return not (self.renamed or self.merged or self.products)


def _row(b: Brand) -> dict:
    return {
        "id": b.id, "name": b.name, "slug": b.slug, "search_term": b.search_term, "logo_path": b.logo_path,
        "description": b.description, "is_active": b.is_active, "sort_order": b.sort_order,
    }


def _keeper_order(b: Brand) -> tuple:
    """Sorts the brand that stays first."""
    return (
        b.logo_path is None,
        not b.is_active,
        not (b.description or "").strip(),
        b.name != brand_caps(b.name.strip()),
        b.sort_order,
        b.id,
    )


async def run(db: AsyncSession, *, apply: bool, scope: str | None = None) -> Plan:
    """Plan the change and, with `apply`, make it (not committed: the caller does).

    `scope` (tests only) limits it to brands whose slug, and products whose
    brand name, contains that text.
    """
    brand_q = select(Brand).order_by(Brand.id)
    product_q = select(Product.id, Product.brand, Product.brand_id).where(Product.brand.is_not(None)).order_by(Product.id)
    if scope:
        brand_q = brand_q.where(Brand.slug.contains(scope))
        product_q = product_q.where(Product.brand.icontains(scope))
    brands = list((await db.scalars(brand_q)).all())
    product_rows = (await db.execute(product_q)).all()

    plan = Plan()
    groups: dict[str, list[Brand]] = {}
    for b in brands:
        groups.setdefault(brand_caps(b.name.strip()), []).append(b)

    moved: dict[int, int] = {}  # dropped brand id → kept brand id
    final_name: dict[int, str] = {}  # kept brand id → its name after
    keep_updates: dict[int, dict] = {}  # kept brand id → column values to write
    touched: dict[int, Brand] = {}
    for name, group in groups.items():
        group.sort(key=_keeper_order)
        keep, drops = group[0], group[1:]
        final_name[keep.id] = name
        values: dict = {}
        if keep.name != name:
            values["name"] = name
            plan.renamed.append({"id": keep.id, "slug": keep.slug, "before": keep.name, "after": name})
        if drops:
            for d in drops:
                moved[d.id] = keep.id
                touched[d.id] = d
            # The first of the others (in the same order) to have one.
            if keep.logo_path is None and (logo := next((d.logo_path for d in drops if d.logo_path), None)):
                values["logo_path"] = logo
            if not (keep.description or "").strip() and (
                desc := next((d.description for d in drops if (d.description or "").strip()), None)
            ):
                values["description"] = desc
            plan.merged.append({
                "name": name,
                "keep": {"id": keep.id, "slug": keep.slug, "name": keep.name},
                "drop": [
                    {"id": d.id, "slug": d.slug, "name": d.name, "products": sum(1 for r in product_rows if r.brand_id == d.id)}
                    for d in drops
                ],
            })
        if values:
            keep_updates[keep.id] = values
            touched[keep.id] = keep
    plan.brands_before = [_row(b) for b in sorted(touched.values(), key=lambda b: b.id)]

    for r in product_rows:
        brand_id = moved.get(r.brand_id, r.brand_id) if r.brand_id is not None else None
        name = final_name.get(brand_id) if brand_id is not None else None
        after = name if name is not None else brand_caps(r.brand.strip())
        if after != r.brand or brand_id != r.brand_id:
            plan.products.append({
                "id": r.id, "before": r.brand, "after": after, "brand_id_before": r.brand_id, "brand_id_after": brand_id,
            })

    if not apply or plan.empty:
        return plan

    # Writes last, and none of them waits long behind the storefront's reads.
    await db.execute(text("SET LOCAL lock_timeout = '5s'"))
    if plan.products:
        # Core statements on the table: the model's brand-linking hook (which
        # looks the name up again) stays out of it; the values are final.
        await db.execute(
            update(Product.__table__)
            .where(Product.__table__.c.id == bindparam("pid"))
            .values(brand=bindparam("new_brand"), brand_id=bindparam("new_brand_id")),
            [{"pid": p["id"], "new_brand": p["after"], "new_brand_id": p["brand_id_after"]} for p in plan.products],
        )
    if moved:
        await db.execute(delete(Brand.__table__).where(Brand.__table__.c.id.in_(list(moved))))
    for brand_id, values in keep_updates.items():
        await db.execute(update(Brand.__table__).where(Brand.__table__.c.id == brand_id).values(**values))
    return plan


def _print(plan: Plan) -> None:
    print(f"Brands renamed: {len(plan.renamed)}")
    for r in plan.renamed:
        print(f"  #{r['id']:<5} {r['before']!r:40} → {r['after']!r}")
    print(f"Duplicates merged: {len(plan.merged)}")
    for m in plan.merged:
        drops = ", ".join(f"#{d['id']} /{d['slug']} ({d['name']!r}, {d['products']} products)" for d in m["drop"])
        print(f"  {m['name']!r}: keeps #{m['keep']['id']} /{m['keep']['slug']}; deletes {drops}")
    print(f"Products whose brand name changes: {len(plan.products)}")
    for p in plan.products[:30]:
        moved = f"  (brand #{p['brand_id_before']} → #{p['brand_id_after']})" if p["brand_id_before"] != p["brand_id_after"] else ""
        print(f"  #{p['id']:<6} {p['before']!r:40} → {p['after']!r}{moved}")
    if len(plan.products) > 30:
        print(f"  … and {len(plan.products) - 30} more")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Brand names in capitals (i → I); merges same-name brands.")
    parser.add_argument("--apply", action="store_true", help="write the change (default: only show it)")
    args = parser.parse_args()

    from app.core.database import async_session, engine

    engine.echo = False
    async with async_session() as db:
        plan = await run(db, apply=args.apply)
        _print(plan)
        if plan.empty:
            print("Nothing to do.")
            return
        if not args.apply:
            print("\nDry run: nothing written. Run with --apply to write.")
            return
        REPORTS.mkdir(exist_ok=True)
        report = REPORTS / f"brand-caps-{datetime.now():%Y%m%d-%H%M%S}.json"
        report.write_text(json.dumps(asdict(plan), ensure_ascii=False, indent=1))
        await db.commit()
        print(f"\nWritten. Rows as they were: {report}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
