"""English and Arabic for the catalog, written by Claude, approved by an admin.

When a product or category is saved without a language (or with a draft that
was written from Turkish since edited), Claude writes it from the Turkish —
name, description, and for a product its SEO title and description — under
the shop's translation brief (translation_brief.md: the trade's own
vocabulary, brand names and codes untouched, the HTML kept tag for tag, no
invented facts). The result is stored as a *draft* (app.schemas.i18n): the
storefront shows it at once, but the page stays noindex and out of the sitemap
and hreflang until an admin approves it in "Çeviriler". A reviewed
translation is never rewritten here.

Two ways in: the admin's save schedules the row (FastAPI background task), and
a sweep every 15 minutes catches whatever that missed — a restart, an outage
of the API, a product added by a script. Off entirely without
ANTHROPIC_API_KEY.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.database import async_session
from app.models.catalog import Category, Product
from app.schemas.i18n import TRANSLATED_LOCALES, CategoryTranslation, ProductTranslation

logger = logging.getLogger(__name__)

BRIEF = (Path(__file__).with_name("translation_brief.md")).read_text(encoding="utf-8")

PRODUCT_FIELDS = ("name", "description", "meta_title", "meta_description")
CATEGORY_FIELDS = ("name", "description")
LANGUAGE_NAME = {"en": "English", "ar": "Arabic"}

#: Rows being translated in this process right now, so a save and the sweep
#: never pay for the same row twice.
_busy: set[str] = set()


def enabled() -> bool:
    return bool(settings.ANTHROPIC_API_KEY)


def fingerprint(fields: dict[str, Any]) -> str:
    """The Turkish a draft was written from, in 16 hex characters."""
    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def needed(translations: dict | None, source: str) -> list[str]:
    """Languages to (re)write: missing, or a draft of Turkish that has changed. Reviewed ones never."""
    out = []
    for lang in TRANSLATED_LOCALES:
        t = (translations or {}).get(lang) or {}
        if not t.get("name") or (t.get("draft") and t.get("source") != source):
            out.append(lang)
    return out


def _tags(html: str | None) -> list[str]:
    """The element names of a description, in order: what a translation must keep."""
    return re.findall(r"<\s*([a-zA-Z0-9]+)", html or "")


def _tool(languages: list[str], fields: tuple[str, ...]) -> dict:
    field_schema = {
        "name": {"type": "string"},
        "description": {"type": ["string", "null"], "description": "The translated HTML, same tags; null when the Turkish has none."},
        "meta_title": {"type": "string", "description": "At most 60 characters (70 hard)."},
        "meta_description": {"type": "string", "description": "120 to 155 characters (160 hard)."},
    }
    one = {"type": "object", "properties": {f: field_schema[f] for f in fields}, "required": list(fields)}
    return {
        "name": "submit_translation",
        "description": "Submit the translation, one object per requested language.",
        "input_schema": {"type": "object", "properties": {lang: one for lang in languages}, "required": languages},
    }


async def complete(system: str, prompt: str, tool: dict) -> dict:
    """One Claude call, its answer forced through the tool. (Tests replace this.)"""
    import anthropic  # here, so the app starts without the package when the feature is off

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=120)
    message = await client.messages.create(
        model=settings.AUTO_TRANSLATE_MODEL,
        max_tokens=16000,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in message.content:
        if block.type == "tool_use":
            return block.input
    raise ValueError("no tool call in Claude's answer")


async def _write(kind: str, source: dict[str, Any], languages: list[str]) -> dict[str, dict]:
    """Claude's translation of `source` into `languages`, validated; a failing language is left out."""
    fields = PRODUCT_FIELDS if kind == "product" else CATEGORY_FIELDS
    model = ProductTranslation if kind == "product" else CategoryTranslation
    wanted = ", ".join(LANGUAGE_NAME[lang] for lang in languages)
    prompt = (
        f"Translate this {kind} of the shop from Turkish into {wanted}, following the brief.\n\n"
        + json.dumps({"name": source["name"], "description_html": source.get("description")}, ensure_ascii=False, indent=1)
    )
    result: dict[str, dict] = {}
    feedback = ""
    for _attempt in range(2):
        answer = await complete(BRIEF, prompt + feedback, _tool(languages, fields))
        problems = []
        for lang in languages:
            if lang in result:
                continue
            raw = {k: answer.get(lang, {}).get(k) for k in fields}
            if source.get("description") and raw.get("description") and _tags(raw["description"]) != _tags(source["description"]):
                problems.append(f"{lang}: the description's HTML tags differ from the Turkish; keep every tag, in order")
                continue
            try:
                result[lang] = model.model_validate({**raw, "draft": True}).model_dump(exclude_none=True)
            except ValidationError as exc:
                problems.append(f"{lang}: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}")
        if not problems:
            break
        feedback = "\n\nYour previous answer was rejected: " + "; ".join(problems) + ". Answer again."
        languages = [lang for lang in languages if lang not in result]
    return result


async def translate(kind: str, row_id: Any, factory: async_sessionmaker | None = None) -> list[str]:
    """Write the missing languages of one product or category. Returns the languages written."""
    if not enabled():
        return []
    key = f"{kind}:{row_id}"
    if key in _busy:
        return []
    _busy.add(key)
    model, fields = (Product, PRODUCT_FIELDS) if kind == "product" else (Category, CATEGORY_FIELDS)
    try:
        async with (factory or async_session)() as db:
            row = await db.get(model, row_id)
            if row is None:
                return []
            source = {f: getattr(row, f) for f in fields if getattr(row, f, None)}
            mark = fingerprint(source)
            languages = needed(row.translations, mark)
            if not languages:
                return []
        # The call is slow; no session is held open across it.
        written = await _write(kind, source, languages)
        if not written:
            return []
        async with (factory or async_session)() as db:
            row = await db.get(model, row_id)
            if row is None:
                return []
            # Fresh read: an admin may have filled a language meanwhile, or
            # changed the Turkish (then this draft is already stale — skip).
            if fingerprint({f: getattr(row, f) for f in fields if getattr(row, f, None)}) != mark:
                return []
            current = dict(row.translations or {})
            for lang in list(written):
                if lang in needed(current, mark):
                    current[lang] = {**written[lang], "source": mark}
                else:
                    written.pop(lang)
            if written:
                row.translations = current
                await db.commit()
        if written:
            logger.info("Auto-translated %s %s into %s", kind, row_id, ", ".join(written))
            await _refresh_storefront()
        return list(written)
    except Exception:
        logger.exception("Auto-translation of %s %s failed", kind, row_id)
        return []
    finally:
        _busy.discard(key)


async def sweep(limit: int | None = None, factory: async_sessionmaker | None = None) -> int:
    """Translate what still lacks a language: published products, active categories. Returns rows done."""
    if not enabled():
        return 0
    limit = limit or settings.AUTO_TRANSLATE_SWEEP_LIMIT
    todo: list[tuple[str, Any]] = []
    async with (factory or async_session)() as db:
        for kind, model, fields, live in (
            ("category", Category, CATEGORY_FIELDS, Category.is_active.is_(True)),
            ("product", Product, PRODUCT_FIELDS, Product.is_published.is_(True)),
        ):
            for row in (await db.scalars(select(model).where(live))).all():
                if needed(row.translations, fingerprint({f: getattr(row, f) for f in fields if getattr(row, f, None)})):
                    todo.append((kind, row.id))
    done = 0
    for kind, row_id in todo[:limit]:
        if await translate(kind, row_id, factory):
            done += 1
    return done


async def _refresh_storefront() -> None:
    """Ask the storefront to drop its cached catalog, so the draft shows now (optional)."""
    if not (settings.STOREFRONT_REVALIDATE_URL and settings.REVALIDATE_SECRET):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(settings.STOREFRONT_REVALIDATE_URL, headers={"X-Revalidate-Secret": settings.REVALIDATE_SECRET})
    except httpx.HTTPError:
        logger.warning("Storefront revalidation failed", exc_info=True)
