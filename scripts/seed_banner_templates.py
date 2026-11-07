"""The special days' banner presets (app.services.banner_templates), written.

    .venv/bin/python scripts/seed_banner_templates.py --dry-run
    .venv/bin/python scripts/seed_banner_templates.py

Adds the templates that are missing and, on the presets already there, fills
in only what is empty (title, look, dates); 10 Kasım is made black, white and
automatic for its day whatever it was. Running it again changes nothing. One
transaction; a write saves what it did to
scripts/migration_reports/banner-templates-<date>.json.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import async_session, engine  # noqa: E402
from app.services import banner_templates  # noqa: E402


async def run(dry_run: bool) -> None:
    async with async_session() as db:
        changes = await banner_templates.apply(db)
        for c in changes:
            detail = f" ({', '.join(c.fields)})" if c.fields else ""
            print(f"{c.action:>9}  {c.title}{detail}")
        if dry_run:
            await db.rollback()
            print("dry run: nothing written")
            return
        await db.commit()
        out = Path(__file__).resolve().parent / "migration_reports" / f"banner-templates-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps([asdict(c) for c in changes], ensure_ascii=False, indent=1))
        print(f"written; report: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = parser.parse_args()

    async def go():
        try:
            await run(args.dry_run)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
