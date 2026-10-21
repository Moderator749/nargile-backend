"""The catalog in the storefront's language.

The public product and category endpoints take `lang` ("en" or "ar"; Turkish
when absent). With it, a row's name and description are its translation
(app.schemas.i18n) and its SEO fields are the translation's own — never the
Turkish ones, which would put Turkish into an English page's <title>. A field
the translation lacks falls back to the Turkish, so a page never goes blank.

Every answer says which languages the row is really translated into
(`locales`, Turkish always first), so the storefront indexes and links an
English or Arabic page only where one exists.
"""

from typing import Any

from app.schemas.i18n import TRANSLATED_LOCALES


def locales_of(translations: dict | None) -> list[str]:
    """"tr", then each language the row has a translated name in."""
    return ["tr", *(lang for lang in TRANSLATED_LOCALES if ((translations or {}).get(lang) or {}).get("name"))]


def _translation(translations: dict | None, lang: str | None) -> dict | None:
    if lang not in TRANSLATED_LOCALES:
        return None
    found = (translations or {}).get(lang) or {}
    return found if found.get("name") else None


def product_view(view: dict[str, Any], translations: dict | None, lang: str | None) -> dict[str, Any]:
    """A product read (as a dict) in `lang`, with its `locales`."""
    view["locales"] = locales_of(translations)
    t = _translation(translations, lang)
    if t is not None:
        view["name"] = t["name"]
        view["description"] = t.get("description") or view.get("description")
        view["meta_title"] = t.get("meta_title")
        view["meta_description"] = t.get("meta_description")
    return view


def category_view(view: Any, translations: dict | None, lang: str | None) -> Any:
    """A CategoryRead in `lang`, with its `locales` (changed in place)."""
    view.locales = locales_of(translations)
    t = _translation(translations, lang)
    if t is not None:
        view.name = t["name"]
        view.description = t.get("description") or view.description
    return view
