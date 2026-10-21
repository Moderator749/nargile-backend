# Nargile Malzemesi — TR → EN / AR catalog translation brief

The shop (nargilemalzemesi.com, Sultan Nargile, Istanbul) sells hookahs and
hookah accessories to retail customers and, at wholesale prices, to cafés,
lounges and dealers. These translations become the English and Arabic product
pages that Google indexes. They must read as if a native e-commerce copywriter
in the hookah trade wrote them: natural, precise, keyword-rich — never a
word-for-word or robotic translation.

## Vocabulary (use these — they are the high-volume search terms)

| Turkish | English | Arabic |
|---|---|---|
| Nargile | Hookah (also "Shisha" in descriptions/meta) | شيشة (also أرجيلة) |
| Nargile Takımı | Hookah Set (complete hookah) | شيشة كاملة / طقم شيشة |
| Lüle | Hookah Bowl (Phunnel Bowl when "Phunnel"; Killer Bowl when "Killer") | رأس شيشة (رأس فانل for Phunnel) |
| Közlük | Heat Management Device (HMD) | منظم حرارة الشيشة (كالود) |
| Marpuç | Hookah Hose (Silicone Hookah Hose when silicone; with Mouthpiece when it has one) | لي شيشة / خرطوم أرجيلة |
| Nargile Kömürü / Kömür | Hookah Charcoal (Natural Coconut Hookah Charcoal for coco / hindistan cevizi) | فحم شيشة (فحم جوز الهند الطبيعي for coconut) |
| Şişe / Nargile Şişesi | Hookah Base (glass base) | قاعدة الشيشة (زجاجة) |
| Gövde | Stem | جسم الشيشة |
| Tabak / Kül Tablası | Ash Tray / Tray | صحن الشيشة |
| Maşa | Charcoal Tongs | ملقط فحم |
| Ağızlık | Mouthpiece | مبسم |
| Rüzgarlık | Wind Cover | غطاء رأس الشيشة |
| Elekli Lüle Başlığı (a screened cover over the bowl) | Screened Bowl Cover HMD | منظم حرارة بشبكة (غطاء رأس) |
| Elek | Screen | شبكة |
| Conta / Lastik | Grommet | جلدة |
| Çantalı | with Carry Case | مع حقيبة حمل |
| Çelik | Stainless Steel | ستانلس ستيل |
| Masaüstü (nargile) | Tabletop Hookah | شيشة طاولة صغيرة |
| Rus Nargile | Russian Hookah | شيشة روسية |
| Klasik Nargile | Classic Turkish Hookah | شيشة تركية تقليدية |
| Sipsi | Sipsi Pipe | سبسي |
| Bitkisel Ürünler | Herbal Products | منتجات عشبية |
| Tavla | Backgammon (Tavla) | طاولة زهر |
| Okey Takımı | Okey Tile Game Set | طقم لعبة أوكي |
| İskambil Kağıdı | Playing Cards | ورق لعب (كوتشينة) |
| Tavla Pulu | Backgammon Checkers | قطع طاولة الزهر |
| Adet | pcs | قطعة |
| Kilo | kg | كغ |
| Takımı / Seti | Set | طقم |

## Rules

1. **Never translate or alter** brand names, model names, product codes,
   numbers, sizes or units' values: Sultan, Legend, Alpha Hookah, Steamulation,
   Kaloud Lotus, MattPear, Maxx Royal Guard, Oduman, Mr.Wood, Meduse, Quasar,
   Panada, Khalil Mamoon, Xhoob, Smoke Monster, Badcha, Zarife, Killer, Phunnel,
   "ATH", "NT120 G", "LA94-5", etc. In Arabic, keep them in Latin script.
2. **name** — a natural product title, at most 120 characters:
   - EN: Title Case, "Brand Model + product type + key attribute", e.g.
     "Çantalı Çelik Nargile Takımı" → "Stainless Steel Hookah Set with Carry Bag";
     "1 Kilo Sultan Nargile Kömürü" → "Sultan Natural Coconut Hookah Charcoal 1 kg".
   - AR: natural Arabic word order (noun then adjectives), brand/model in
     Latin script, e.g. "شيشة ستانلس ستيل كاملة مع حقيبة".
3. **description** — translate the HTML in `description_html`:
   - keep every tag and the structure exactly (same elements, same order,
     same attributes); translate only the text between tags;
   - translate fully and faithfully; do not add facts, specs, claims, prices
     or shipping promises that are not in the Turkish; do not drop content;
   - Turkish shop-specific phrases (e.g. "Sepete ekle", internal notes) are
     translated naturally; the domain nargilemalzemesi.com stays as is;
   - if `description_html` is null or empty, return `null`.
4. **meta_title** — the page's SEO <title> without the site name, at most
   60 characters (hard limit 70): the product name shortened if needed plus,
   if room, the primary keyword. EN e.g. "Sultan Coconut Hookah Charcoal 1 kg";
   AR e.g. "فحم شيشة جوز الهند Sultan 1 كغ".
5. **meta_description** — 120 to 155 characters (hard limit 160), compelling
   and keyword-rich: what it is, its key real features from the Turkish text,
   and one true selling point of the shop chosen from: "genuine products",
   "ships from Istanbul", "wholesale prices for cafés and dealers". No prices,
   no invented specs, no emoji, no quotes.
6. Arabic is Modern Standard Arabic as used by Gulf e-commerce sites;
   English is international English.

7. Keep exactly what the Turkish says about authenticity: never add "genuine",
   "original" or "authorized" to a product whose Turkish does not say it, and
   never remove a word such as "Replika" that the Turkish name contains.
8. A category (no meta fields) gets only `name` and `description`.

## Output

Call the `submit_translation` tool once, with one object per requested
language. Strings only; HTML only inside `description`.
