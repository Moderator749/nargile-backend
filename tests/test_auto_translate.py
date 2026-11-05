"""Claude's English and Arabic drafts: written for what lacks them, shown but
not indexed until approved, rewritten when the Turkish changes, never over a
reviewed translation. Claude itself is replaced by a stub."""

import sys
import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.catalog import Product
from app.models.user import User
from app.services import auto_translate
from tests.conftest import auth_header
from tests.test_product_sheet import _post, _sheet

#: The real call, before conftest's no_claude_calls swaps it for a refusal.
REAL_COMPLETE = auto_translate.complete

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


# ── The call itself, against a stand-in for the anthropic package ──

TOOL = auto_translate._tool(["en"], auto_translate.CATEGORY_FIELDS)


def fake_anthropic(monkeypatch, *answers: list) -> list[dict]:
    """`import anthropic` inside complete() gets a client answering `answers` in turn; returns the requests."""
    requests: list[dict] = []

    class Messages:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(content=answers[len(requests) - 1], stop_reason="end_turn")

    class AsyncAnthropic:
        def __init__(self, **kwargs):
            self.messages = Messages()

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(AsyncAnthropic=AsyncAnthropic))
    return requests


def tool_call(**input):
    return SimpleNamespace(type="tool_use", name="submit_translation", input=input)


THINKING = SimpleNamespace(type="thinking", thinking="…")
PROSE = SimpleNamespace(type="text", text="Here is the translation: Oduman Pipes")


async def test_the_model_picks_the_tool_and_its_thinking_is_skipped(monkeypatch):
    requests = fake_anthropic(monkeypatch, [THINKING, tool_call(en={"name": "Oduman Pipes"})])
    assert await REAL_COMPLETE("brief", "Translate", TOOL) == {"en": {"name": "Oduman Pipes"}}
    (request,) = requests
    assert request["tool_choice"] == {"type": "auto"}  # a forced tool_choice is refused by Opus 5.5
    assert request["model"] == settings.AUTO_TRANSLATE_MODEL
    assert request["system"].startswith("brief") and "only by calling the submit_translation tool" in request["system"]


async def test_an_answer_without_the_tool_is_asked_for_once_more(monkeypatch):
    requests = fake_anthropic(monkeypatch, [THINKING, PROSE], [tool_call(en={"name": "Oduman Pipes"})])
    assert await REAL_COMPLETE("brief", "Translate", TOOL) == {"en": {"name": "Oduman Pipes"}}
    first, second = (r["messages"][0]["content"] for r in requests)
    assert first == "Translate" and second.startswith("Translate") and "did not call the submit_translation tool" in second


async def test_two_answers_without_the_tool_fail(monkeypatch):
    requests = fake_anthropic(monkeypatch, [PROSE], [PROSE])
    with pytest.raises(ValueError, match="no tool call"):
        await REAL_COMPLETE("brief", "Translate", TOOL)
    assert len(requests) == 2


def test_no_test_reaches_claude():
    """conftest's guard: the feature is off, and the call is a refusal (which, called, fails the test)."""
    assert not auto_translate.enabled()
    assert auto_translate.complete is not REAL_COMPLETE
