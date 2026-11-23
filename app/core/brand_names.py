"""Brand names in capitals, the way the admin panel enters them.

The storefront shows a brand's name exactly as stored, and the panel's brand
boxes put what is typed into capitals as it is typed (saas_frontend
src/lib/brandCase.ts — the same rule, kept in step with this one). The rule is
the neutral (English) one: "i" becomes "I", never the Turkish "İ", since brands
are foreign names ("SHISHA FOIL", not "SHİSHA FOİL"). A Turkish capital that is
already there ("İ", "Ğ", "Ş"…) stays, and a small Turkish letter takes its own
capital ("ğ" → "Ğ", "ı" → "I").

Letter by letter, and a letter whose capital is longer than itself ("ß" →
"SS") is left alone, so a name never grows past its column.
"""


def brand_caps(name: str) -> str:
    out = []
    for ch in name:
        up = ch.upper()
        out.append(up if len(up) == 1 else ch)
    return "".join(out)
