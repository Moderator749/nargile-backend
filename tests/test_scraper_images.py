"""Tests for product image extraction in the live catalog scraper."""

from bs4 import BeautifulSoup

from scripts.scrape_live_catalog import extract_image_url

IMG = "https://www.nargilemalzemesi.com/image/cache/catalog/urunler/komurler/sultan"


def _card(img_attrs: str):
    html = f'<div class="product-layout"><div class="image"><a><img {img_attrs}></a></div></div>'
    return BeautifulSoup(html, "html.parser").select_one(".product-layout")


def test_prefers_high_resolution_srcset_entry():
    card = _card(
        f'src="https://www.nargilemalzemesi.com/image/cache/transparent-400x400.png" '
        f'data-src="{IMG}-400x400.jpg" '
        f'data-srcset="{IMG}-400x400.jpg 1x, {IMG}-800x800.jpg 2x"'
    )
    assert extract_image_url(card) == f"{IMG}-800x800.jpg"


def test_falls_back_to_data_src_and_resolves_relative_urls():
    card = _card('src="/image/cache/transparent-400x400.png" data-src="/image/cache/catalog/a-400x400.jpg"')
    assert extract_image_url(card) == "https://www.nargilemalzemesi.com/image/cache/catalog/a-400x400.jpg"


def test_placeholder_only_or_missing_image_is_none():
    assert extract_image_url(_card('src="/image/cache/transparent-400x400.png"')) is None
    no_img = BeautifulSoup('<div class="product-layout"></div>', "html.parser").select_one("div")
    assert extract_image_url(no_img) is None


def test_rejects_urls_unsafe_for_css_or_html():
    assert extract_image_url(_card('data-src="https://x.com/a.jpg&quot;)"')) is None
