"""Claude's English and Arabic drafts: written for what lacks them, shown but
not indexed until approved, rewritten when the Turkish changes, never over a
reviewed translation. Claude itself is replaced by a stub."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.catalog import Product
from app.models.user import User
from app.services import auto_translate
from tests.conftest import auth_header
from tests.test_product_sheet import _post, _sheet

ANSWER = {
    "en": {
        "name": "Diamond Cut Hookah Set",
        "description": "<p>A <strong>stainless</strong> hookah.</p>",
        "meta_title": "Diamond Cut Hookah Set",
        "meta_description": "Diamond cut stainless steel hookah set with a crystal base. Genuine products shipped from Istanbul at wholesale prices for cafés.",
    },
    "ar": {
        "name": "شيشة كاملة بقطع الماس",
        "description": "<p>شيشة <strong>ستانلس</strong>.</p>",
        "meta_title": "شيشة كاملة بقطع الماس",
        "meta_description": "شيشة ستانلس ستيل كاملة بقطع الماس مع قاعدة كريستال. منتجات أصلية تُشحن من إسطنبول بأسعار جملة للمقاهي والموزعين.",
    },
}


@pytest.fixture
def claude(monkeypatch):
    """Turns the feature on and answers for Claude: from `claude.answers` in turn, else ANSWER."""
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(settings, "STOREFRONT_REVALIDATE_URL", None)

    class Stub:
        def __init__(self):
            self.answers: list[dict] = []
            self.calls: list[dict] = []

    stub = Stub()

    async def fake(system, prompt, tool):
        languages = tool["input_schema"]["required"]
        stub.calls.append({"prompt": prompt, "languages": languages})
        answer = stub.answers.pop(0) if stub.answers else ANSWER
        return {lang: answer[lang] for lang in languages if lang in answer}

    monkeypatch.setattr(auto_translate, "complete", fake)
    return stub


def factory(db: AsyncSession):
    return async_sessionmaker(db.bind, expire_on_commit=False)


async def _product(client, admin_user, **overrides) -> dict:
    """A product saved through the sheet; its background task runs before the response returns."""
    body = _sheet(description="<p>Bir <strong>paslanmaz</strong> nargile.</p>", **overrides)
    created = await _post(client, admin_user, body)
    assert created.status_code == 201, created.text
    return created.json()


async def _translations(db: AsyncSession, pid: uuid.UUID) -> dict:
    row = await db.get(Product, pid)
    await db.refresh(row)
    return row.translations or {}


async def test_a_save_drafts_what_is_missing_shown_but_not_indexed_until_approved(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, claude
):
    product = await _product(client, admin_user)
    pid = uuid.UUID(product["id"])
    saved = await _translations(db_session, pid)
    assert saved["en"]["draft"] is True and saved["en"]["source"] and saved["ar"]["draft"] is True

    en = (await client.get(f"/api/v1/products/{product['slug']}", params={"lang": "en"})).json()
    assert en["name"] == "Diamond Cut Hookah Set"  # shown…
    assert en["locales"] == ["tr"]  # …but not counted: the page stays noindex

    drafts = (await client.get("/api/v1/admin/translations/drafts", headers=auth_header(admin_user))).json()
    assert {"kind": "product", "id": str(pid), "name": product["name"], "languages": ["ar", "en"]} in drafts

    # The admin approves English in "Çeviriler": the sheet saves it back as not a draft.
    sheet = (await client.get(f"/api/v1/admin/products/{pid}/sheet", headers=auth_header(admin_user))).json()
    sheet["translations"]["en"]["draft"] = False
    assert (await client.put(f"/api/v1/admin/products/{pid}/sheet", headers=auth_header(admin_user), json=sheet)).status_code == 200
    en = (await client.get(f"/api/v1/products/{product['slug']}", params={"lang": "en"})).json()
    assert en["locales"] == ["tr", "en"]
    assert "draft" not in (await _translations(db_session, pid))["en"]  # stored as plain text
    # Arabic is still a draft of the same Turkish: not written again.
    assert len(claude.calls) == 1


async def test_a_reviewed_translation_is_never_rewritten_and_a_stale_draft_is(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, claude
):
    reviewed = {"name": "Hand-written Hookah Set"}
    product = await _product(client, admin_user, translations={"en": reviewed})
    pid = uuid.UUID(product["id"])
    assert claude.calls[-1]["languages"] == ["ar"]  # only what was missing
    first = (await _translations(db_session, pid))["ar"]["source"]

    # The Turkish changes: the Arabic draft is stale and rewritten; the reviewed English is not.
    sheet = (await client.get(f"/api/v1/admin/products/{pid}/sheet", headers=auth_header(admin_user))).json()
    sheet["name"] = "Elmas Kesim Nargile Takımı Yeni"
    assert (await client.put(f"/api/v1/admin/products/{pid}/sheet", headers=auth_header(admin_user), json=sheet)).status_code == 200
    assert claude.calls[-1]["languages"] == ["ar"] and len(claude.calls) == 2
    now = await _translations(db_session, pid)
    assert now["ar"]["source"] != first
    assert now["en"] == reviewed
    assert await auto_translate.translate("product", pid, factory(db_session)) == []  # nothing left to do


async def test_a_bad_answer_is_retried_once_and_never_stored(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, claude
):
    lost_tags = {**ANSWER, "en": {**ANSWER["en"], "description": "<p>No bold here.</p>"}}
    too_long = {**ANSWER, "en": {**ANSWER["en"], "meta_title": "x" * 80}}

    # English drops a tag (rejected), Arabic is fine; the retry, for English only, fixes it.
    claude.answers = [lost_tags, ANSWER]
    product = await _product(client, admin_user)
    assert claude.calls[-1]["languages"] == ["en"] and "rejected" in claude.calls[-1]["prompt"]
    assert set(await _translations(db_session, uuid.UUID(product["id"]))) == {"en", "ar"}

    # Two bad answers in a row: nothing is stored for that language.
    claude.answers = [too_long, too_long]
    other = await _product(client, admin_user)
    assert set(await _translations(db_session, uuid.UUID(other["id"]))) == {"ar"}


async def test_off_without_a_key(client: AsyncClient, admin_user: User, db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    product = await _product(client, admin_user)
    assert await _translations(db_session, uuid.UUID(product["id"])) == {}
    assert await auto_translate.sweep(factory=factory(db_session)) == 0


async def test_the_sweep_catches_what_a_save_missed_up_to_its_limit(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    for _ in range(3):
        await _product(client, admin_user)  # saved while the feature was off

    async def fake(system, prompt, tool):
        return {lang: ANSWER[lang] for lang in tool["input_schema"]["required"]}

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(auto_translate, "complete", fake)
    assert await auto_translate.sweep(limit=2, factory=factory(db_session)) == 2
