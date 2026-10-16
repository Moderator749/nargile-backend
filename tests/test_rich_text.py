"""Rich text from the panel's editor: only safe HTML is stored, whoever sends it."""

import pytest
from httpx import AsyncClient

from app.core.rich_text import html_to_text, sanitize_html, text_to_html, to_rich_html
from app.models.user import User
from tests.conftest import auth_header

ATTACKS = [
    '<p>a<script>alert(1)</script></p>',
    '<p><img src=x onerror="alert(1)"></p>',
    '<p><a href="javascript:alert(1)">x</a></p>',
    '<p><a href=" JaVaScRiPt:alert(1)">x</a></p>',
    '<p><a href="data:text/html;base64,PHNjcmlwdD4=">x</a></p>',
    '<p onclick="alert(1)" style="background:url(javascript:alert(1))">x</p>',
    '<p><iframe src="https://evil.example"></iframe>x</p>',
    '<p><svg onload="alert(1)"></svg>x</p>',
    '<p><style>*{display:none}</style>x</p>',
    '<p><!-- <script>alert(1)</script> -->x</p>',
    '<p><a href="https://ok.example" target="_blank" onmouseover="alert(1)">x</a></p>',
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_attacks_are_stripped(attack: str):
    cleaned = sanitize_html(attack)
    lowered = cleaned.lower()
    for bad in ("<script", "<img", "<iframe", "<svg", "<style", "javascript:", "data:", "onerror", "onclick",
                "onload", "onmouseover", "style=", "target=", "<!--"):
        assert bad not in lowered, (attack, cleaned)


def test_the_editors_tags_survive():
    value = (
        '<h2>Başlık</h2><h3>Alt</h3><p><strong>k</strong> <b>k</b> <em>i</em> <i>i</i> <u>a</u><br>yeni</p>'
        '<ul><li>bir</li></ul><ol><li>iki</li></ol><p><a href="https://x.example/y">dış</a> <a href="/catalog/luleler">iç</a></p>'
    )
    cleaned = sanitize_html(value)
    for tag in ("<h2>", "<h3>", "<strong>", "<b>", "<em>", "<i>", "<u>", "<br>", "<ul>", "<ol>", "<li>"):
        assert tag in cleaned
    assert '<a href="https://x.example/y" rel="noopener noreferrer">' in cleaned
    assert '<a href="/catalog/luleler" rel="noopener noreferrer">' in cleaned


def test_headings_outside_the_two_levels_become_text():
    assert sanitize_html("<h1>Ad</h1><p>x</p>") == "Ad<p>x</p>"


def test_plain_text_becomes_paragraphs_and_is_escaped():
    assert text_to_html("## Başlık\n\nbir\niki <b>\n\n\nüç") == "<h2>Başlık</h2><p>bir<br>iki &lt;b&gt;</p><p>üç</p>"
    # A "<b>" typed into plain text is shown, not obeyed.
    assert to_rich_html("not <b>bold</b>") == "<p>not &lt;b&gt;bold&lt;/b&gt;</p>"


def test_empty_editor_output_is_nothing():
    assert to_rich_html("<p></p><p><br></p><p>&nbsp;</p>") is None
    assert to_rich_html("   ") is None
    assert to_rich_html("<p><script>x</script></p>") is None


def test_html_to_text():
    assert html_to_text("<h2>A</h2><p>b<br>c &amp; d</p><ul><li>e</li></ul>") == "A b c & d e"


async def test_legal_text_is_sanitised_through_the_api(client: AsyncClient, admin_user: User):
    resp = await client.put(
        "/api/v1/admin/settings/legal/TERMS",
        headers=auth_header(admin_user),
        json={"title": "Şartlar", "body": '<h2>Madde 1</h2><p>Metin<script>alert(1)</script> <a href="javascript:x()">y</a></p>'},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == '<h2>Madde 1</h2><p>Metin <a rel="noopener noreferrer">y</a></p>'
    # Nothing readable left: refused, as an empty body is.
    empty = await client.put(
        "/api/v1/admin/settings/legal/TERMS", headers=auth_header(admin_user),
        json={"title": "Şartlar", "body": "<p><script>alert(1)</script></p>"},
    )
    assert empty.status_code == 422


async def test_note_body_is_sanitised_through_the_api(client: AsyncClient, admin_user: User):
    resp = await client.post(
        "/api/v1/admin/notes", headers=auth_header(admin_user),
        json={"title": "XSS", "body": '<p onclick="x()">not<img src=x onerror=alert(1)></p>'},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["body"] == "<p>not</p>"
    await client.delete(f"/api/v1/admin/notes/{resp.json()['id']}", headers=auth_header(admin_user))
