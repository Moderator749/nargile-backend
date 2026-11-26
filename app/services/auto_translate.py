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

A circuit breaker stops the calls no retry can fix: the key refused (401/403)
or the account out of credit. The first such answer pauses the feature, says
so once in the panel's team channel ("Ekip" — sender "Sistem") and in the log,
and stores the pause in shop_settings so a restart neither forgets it nor
announces it again. Every COOLDOWN one row is tried; its success lifts the
pause (and says so), its failure keeps it, with a reminder at most daily.
Before the breaker, the November 2026 key and credit outages logged some
7,500 tracebacks over four days and told nobody.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.database import async_session
from app.models.admin_chat import AdminMessage
from app.models.catalog import Category, Product
from app.schemas.i18n import TRANSLATED_LOCALES, CategoryTranslation, ProductTranslation
from app.services import storefront_cache
from app.services.shop_settings import read_setting, save_setting

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


# ── Circuit breaker ──

#: shop_settings key holding the pause: {"reason", "error", "since", "retry_at", "alerted_at"}.
BREAKER_KEY = "auto_translate_breaker"
#: How long a pause lasts before one row is tried again.
COOLDOWN = timedelta(hours=1)
#: The longest the team goes without a reminder while the pause holds.
REMIND_EVERY = timedelta(hours=24)
#: Who the team channel names as the sender.
SYSTEM_SENDER = "Sistem"

#: The pause as last read or written (None = running); loaded once per process.
_breaker: dict | None = None
_breaker_loaded = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fatal_reason(exc: BaseException) -> str | None:
    """Why no retry can help, in Turkish for the team, or None for an error worth retrying.

    Read off the exception's HTTP status and message, so the anthropic package
    need not be imported here."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if "credit balance" in text or "billing" in text:
        return "Anthropic hesabında kredi kalmadı"
    if status in (401, 403):
        return f"Anthropic API anahtarı reddedildi (HTTP {status})"
    return None


async def _load_breaker(factory: async_sessionmaker | None) -> dict | None:
    global _breaker, _breaker_loaded
    if not _breaker_loaded:
        async with (factory or async_session)() as db:
            raw = await read_setting(db, BREAKER_KEY)
        try:
            _breaker = json.loads(raw) if raw else None
        except ValueError:
            _breaker = None
        _breaker_loaded = True
    return _breaker


async def paused(factory: async_sessionmaker | None = None) -> bool:
    """True while the breaker holds; False once its cooldown is over (the next call is the trial)."""
    state = await _load_breaker(factory)
    return bool(state) and _now() < datetime.fromisoformat(state["retry_at"])


async def _store_breaker(db, state: dict | None) -> None:
    global _breaker, _breaker_loaded
    await save_setting(db, BREAKER_KEY, json.dumps(state) if state else "")
    _breaker, _breaker_loaded = state, True


def _post(db, body: str) -> None:
    db.add(AdminMessage(sender_id=None, sender_name=SYSTEM_SENDER, body=body, created_at=_now()))


async def _trip(reason: str, exc: BaseException, factory: async_sessionmaker | None) -> None:
    """Pause after a fatal answer; tell the team the first time, then at most daily."""
    now = _now()
    state = await _load_breaker(factory)
    first = not state
    remind = not first and now - datetime.fromisoformat(state["alerted_at"]) >= REMIND_EVERY
    state = {
        "reason": reason,
        "error": str(exc)[:300],
        "since": state["since"] if state else now.isoformat(),
        "retry_at": (now + COOLDOWN).isoformat(),
        "alerted_at": now.isoformat() if first or remind else state["alerted_at"],
    }
    async with (factory or async_session)() as db:
        await _store_breaker(db, state)
        if first or remind:
            _post(
                db,
                f"⚠️ Otomatik çeviri durduruldu: {reason}. "
                f"İngilizce/Arapça taslaklar yazılmıyor; sorun giderilince kendiliğinden devam eder "
                f"(saatte bir deneniyor). Kontrol: console.anthropic.com → API Keys / Billing, "
                f"sunucuda ANTHROPIC_API_KEY (değiştirince nargile servisini yeniden başlatın).",
            )
        await db.commit()
    if first:
        logger.error("Auto-translation paused: %s (%s). Retrying hourly; the team was told.", reason, str(exc)[:200])
    else:
        logger.warning("Auto-translation still paused: %s.", reason)


async def _close_breaker(factory: async_sessionmaker | None) -> None:
    """A call went through: lift the pause, if there was one, and say so."""
    if not await _load_breaker(factory):
        return
    async with (factory or async_session)() as db:
        await _store_breaker(db, None)
        _post(db, "✅ Otomatik çeviri yeniden çalışıyor; bekleyen taslaklar sırayla yazılacak.")
        await db.commit()
    logger.info("Auto-translation resumed.")


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


#: Said with every call: the model picks the tool itself (Opus 5.5 refuses a
#: forced tool_choice, "type tool and any are not supported for this model").
TOOL_ONLY = (
    "Answer only by calling the {name} tool, once, with every requested language. "
    "Write no text outside the tool call."
)
#: Added to the prompt when an answer came back without the tool call.
TOOL_NUDGE = "\n\nYour previous answer did not call the {name} tool. Call it now with the translation, and nothing else."


async def complete(system: str, prompt: str, tool: dict) -> dict:
    """One Claude answer, read from its tool call; asked once more if it has none. (Tests replace this.)"""
    import anthropic  # here, so the app starts without the package when the feature is off

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=120)
    name = tool["name"]
    for attempt in range(2):
        message = await client.messages.create(
            model=settings.AUTO_TRANSLATE_MODEL,
            max_tokens=16000,
            system=f"{system}\n\n{TOOL_ONLY.format(name=name)}",
            tools=[tool],
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": prompt + (TOOL_NUDGE.format(name=name) if attempt else "")}],
        )
        # Thinking and text blocks may come first; only the tool call is the answer.
        for block in message.content:
            if block.type == "tool_use" and block.name == name:
                return block.input
        logger.warning("Claude answered without %s (stop_reason %s)", name, message.stop_reason)
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
    if not enabled() or await paused(factory):
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
        await _close_breaker(factory)  # Claude answered: whatever paused it is over
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
            _refresh_storefront()
        return list(written)
    except Exception as exc:
        reason = fatal_reason(exc)
        if reason:
            await _trip(reason, exc, factory)
        else:
            logger.exception("Auto-translation of %s %s failed", kind, row_id)
        return []
    finally:
        _busy.discard(key)


async def sweep(limit: int | None = None, factory: async_sessionmaker | None = None) -> int:
    """Translate what still lacks a language: published products, active categories. Returns rows done."""
    if not enabled() or await paused(factory):
        return 0
    limit = limit or settings.AUTO_TRANSLATE_SWEEP_LIMIT
    todo: list[tuple[str, Any]] = []
    async with (factory or async_session)() as db:
        for kind, model, fields, live in (
            ("category", Category, CATEGORY_FIELDS, Category.is_active.is_(True)),
            ("product", Product, PRODUCT_FIELDS, Product.on_storefront()),
        ):
            for row in (await db.scalars(select(model).where(live))).all():
                if needed(row.translations, fingerprint({f: getattr(row, f) for f in fields if getattr(row, f, None)})):
                    todo.append((kind, row.id))
    done = 0
    for kind, row_id in todo[:limit]:
        if await paused(factory):  # a fatal answer midway: stop, rather than try every row
            break
        if await translate(kind, row_id, factory):
            done += 1
    return done


def _refresh_storefront() -> None:
    """Ask the storefront to drop its cached catalog, so the draft shows soon
    (optional; gathered with every other request, see storefront_cache)."""
    storefront_cache.request_refresh()
