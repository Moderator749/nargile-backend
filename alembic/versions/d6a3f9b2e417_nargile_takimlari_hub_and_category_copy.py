"""Nargile Takımları over the takım categories, and each category's own words

"nargile takımı" is the shop's main search and no page answered it: the sets
were spread over five kinds (Klasik, Çantalı, Çelik, Masaüstü, Rus) and the
house brands, each a category on its own, and the top menu's "Nargile
Takımları" had nowhere to go. This puts them under one parent —
/catalog/nargile-takimlari, every set the shop sells on one page, its
subcategories a click away — and adds it to the top menu and the head of the
category column. Quasar stays where it is: most of what it lists are bowls and
parts, and its hookahs are already under Masaüstü.

And the categories' own words, in place of the shop's one passage closing
every listing page (the same text on every page, which search engines read as
one page said many times): an introduction (with its English and Arabic,
where the category is translated) and a closing passage (Turkish only, as the
panel's). Written from what each category sells, without prices: those change.
The storefront's own closing passage is rewritten in the same commit; its
English and Arabic go into the homepage's translation memory here.

Only what is empty is written. A category the admin has already given a
parent, an introduction or a passage keeps it; so does a menu that already has
the entry. Downgrade takes back exactly what is still as written here.

Revision ID: d6a3f9b2e417
Revises: c4e8a1f7d290
Create Date: 2026-10-28
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6a3f9b2e417"
down_revision: Union[str, None] = "c4e8a1f7d290"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

HUB = "nargile-takimlari"
HUB_NAME = {"tr": "Nargile Takımları", "en": "Hookah Sets", "ar": "أطقم الشيشة"}

# Under the hub: the five kinds first, then the house and premium brands.
CHILDREN = [
    "rus-takimlar",
    "celik-takimlar",
    "klasik-takimlar",
    "masaustu-takimlar",
    "cantali-nargile-takimlari",
    "sultan",
    "legend",
    "oduman",
    "mr-wood",
    "steamulation",
    "meduse",
    "panada",
]

# A menu entry with no label of its own takes the category's name, in the
# page's language (lib/storefrontNav: resolveNav).
NAV_ENTRY = {"label": "", "slug": HUB, "accent": False, "enabled": True}
NAV_TOP_MAX, NAV_SIDEBAR_MAX = 12, 30

# slug → {"tr", "en", "ar": introduction (HTML); "seo_title", "seo_text": closing passage}.
# An introduction's first paragraph is its meta description: under ~155 characters.
COPY: dict[str, dict[str, str]] = {
    HUB: {
        "tr": (
            "<p>Rus, çelik, klasik, masaüstü ve çantalı nargile takımı modelleri bir arada: Sultan, Legend, "
            "Alpha Hookah ve Steamulation gibi markaların orijinal takımları.</p>"
            "<p>Evde keyifle kullanacağınız kompakt bir takım da, kafenizde her gün çalışacak dayanıklı bir model "
            "de burada. Türlerine göre ayrılmış kategorilerden aradığınız takıma hızla ulaşabilir, kafe, toptancı "
            "ve bayilere özel fiyatlarımızdan yararlanabilirsiniz.</p>"
        ),
        "en": (
            "<p>Russian-style, stainless steel, classic, tabletop and travel hookah sets in one place: original "
            "sets from Sultan, Legend, Alpha Hookah, Steamulation and more.</p>"
            "<p>From a compact hookah for home to a hard-working set for your lounge, compare every model by "
            "type and brand. Cafés, wholesalers and dealers get prices of their own.</p>"
        ),
        "ar": (
            "<p>شيشة روسية وستانلس ستيل وكلاسيكية وشيشة طاولة وشيشة مع حقيبة في مكان واحد: أطقم أصلية من "
            "Sultan وLegend وAlpha Hookah وSteamulation وغيرها.</p>"
            "<p>من شيشة صغيرة للاستخدام المنزلي إلى طقم متين للمقهى، قارن جميع الموديلات حسب النوع والماركة. "
            "أسعار خاصة للمقاهي وتجار الجملة والموزعين.</p>"
        ),
        "seo_title": "Nargile Takımı Seçerken Bilmeniz Gerekenler",
        "seo_text": (
            "<p>Nargile takımı; gövde, şişe, lüle ve marpuçtan oluşan, birbiriyle uyumlu çalışan parçaların "
            "bütünüdür. Doğru takım rahat bir çekiş, dengeli bir duman ve uzun yıllar sorunsuz kullanım demektir. "
            "Aşağıda nargile takımı çeşitlerini, seçerken nelere dikkat etmeniz gerektiğini ve en çok sorulan "
            "soruları bir araya getirdik.</p>"
            "<h3>Nargile takımı çeşitleri</h3>"
            "<ul>"
            '<li><a href="/catalog/rus-takimlar">Rus nargile takımları</a>: paslanmaz çelik veya alüminyum '
            "gövdeli, modern tasarımlı modeller. Hafif çekişleri, sökülüp kolayca temizlenen parçaları ve geniş "
            "aksesuar uyumlarıyla hem evde hem kafede en çok tercih edilen takımlardır.</li>"
            '<li><a href="/catalog/celik-takimlar">Çelik nargile takımları</a>: paslanmaya dayanıklı gövdeleriyle '
            "uzun ömürlü, bakımı kolay takımlar.</li>"
            '<li><a href="/catalog/klasik-takimlar">Klasik nargile takımları</a>: geleneksel Türk nargilesinin '
            "işlemeli gövdesini ve geniş cam şişesini sevenler için.</li>"
            '<li><a href="/catalog/masaustu-takimlar">Masaüstü nargile takımları</a>: kısa boylu, kompakt ve '
            "sağlam duruşlu; balkon, teras ve küçük alanlar için.</li>"
            '<li><a href="/catalog/cantali-nargile-takimlari">Çantalı nargile takımları</a>: parçaları korunaklı '
            "bir çantada taşınan, seyahatte ve piknikte yanınızda olan modeller.</li>"
            "</ul>"
            "<h3>Nargile takımı alırken nelere dikkat edilmeli?</h3>"
            "<ul>"
            "<li><strong>Boy:</strong> Uzun gövdeli takımlar dumanı daha uzun bir yoldan geçirir ve daha serin bir "
            "içim sunar; kısa ve mini takımlar ise az yer kaplar, kolay taşınır.</li>"
            "<li><strong>Gövde malzemesi:</strong> Paslanmaz çelik uzun ömürlüdür ve kolay temizlenir; alüminyum "
            "hafiftir ve renk seçeneği boldur; işlemeli gövdeler klasik bir görünüm sunar.</li>"
            "<li><strong>Şişe:</strong> Kalın camdan, geniş tabanlı bir şişe takımı devrilmeye karşı daha sağlam "
            "yapar. Şişeyle gövdenin sıkı oturması sızdırmazlığı ve dengeli çekişi sağlar.</li>"
            "<li><strong>Çekiş:</strong> Difüzörlü modellerde ses ve çekiş daha yumuşaktır; tahliye valfi şişede "
            "biriken dumanı tek nefeste boşaltmanızı sağlar.</li>"
            "<li><strong>Yedek parça:</strong> Lüle oturağı, marpuç girişi ve contaları standart ölçüde olan bir "
            'takımın <a href="/catalog/luleler">lülesini</a>, <a href="/catalog/marpuclar">marpucunu</a> ve '
            '<a href="/catalog/siseler">şişesini</a> sonradan kolayca yenileyebilirsiniz.</li>'
            "</ul>"
            "<h3>Kafe ve işletmeler için nargile takımı</h3>"
            "<p>Kafede kullanılacak bir takımda dayanıklılık, kolay temizlik ve yedek parçanın kolay bulunması ilk "
            "sırada gelir. Aynı modelden birden fazla takım alacak kafe, toptancı ve bayiler için özel fiyatlarımız "
            "var: işletme hesabınız onaylandığında fiyatlarınız hesabınıza tanımlanır.</p>"
            "<h3>Sık sorulan sorular</h3>"
            "<p><strong>En iyi nargile takımı hangisi?</strong><br>Herkes için tek bir en iyi takım yok; doğru "
            "seçim kullanacağınız yere ve beklentinize bağlı. Hafif çekiş ve kolay temizlik arıyorsanız Rus tipi "
            "ya da çelik bir takım, geleneksel bir görünüm istiyorsanız klasik bir takım, sık taşıyacaksanız "
            "çantalı bir model iyi bir başlangıçtır.</p>"
            "<p><strong>Nargile takımı fiyatları neye göre değişir?</strong><br>Gövdenin malzemesi ve işçiliği, "
            "şişenin kalitesi, takımla birlikte gelen parçalar (lüle, marpuç, közlük, çanta) ve marka fiyatı "
            "belirleyen başlıca etkenlerdir. Her modelin güncel fiyatı kendi sayfasındadır.</p>"
            "<p><strong>Rus nargile ile klasik nargile arasındaki fark nedir?</strong><br>Rus tipi takımlar modern "
            "malzemeler ve hassas işçilikle, hafif çekiş ve kolay bakım için tasarlanır. Klasik takımlar ise "
            "geleneksel Türk nargilesinin formunu ve el işçiliğini sürdürür.</p>"
            "<p><strong>Takımla birlikte neler geliyor?</strong><br>İçerik modelden modele değişir; çoğu takım "
            "gövde, şişe, lüle ve marpuçla gelir. Takımın içeriğini ürün sayfasındaki açıklamadan kontrol "
            "edebilirsiniz.</p>"
            "<p><strong>Ürünler orijinal mi?</strong><br>Satışa sunduğumuz tüm nargile takımları markalarının "
            "orijinal ürünleridir.</p>"
        ),
    },
    "rus-takimlar": {
        "tr": (
            "<p>Alpha Hookah, Union, Hoob, Maxx Royal, Nube ve Mattpear gibi markaların Rus nargile takımları: "
            "hafif çekiş, paslanmaz gövde ve modern tasarım bir arada.</p>"
            "<p>Rus tipi nargile takımları; sökülüp temizlenebilen parçaları, dengeli çekişi ve geniş aksesuar "
            "uyumuyla evde de kafede de en çok tercih edilen modeller arasında. Aradığınız markayı, rengi ve "
            "fiyat aralığını filtrelerle kolayca bulabilirsiniz.</p>"
        ),
        "en": (
            "<p>Russian-style hookahs from Alpha Hookah, Union, Hoob, Maxx Royal, Nube and Mattpear: a light "
            "draw, a stainless body and modern design.</p>"
            "<p>Russian hookahs come apart for easy cleaning and work with a wide range of bowls, hoses and "
            "heat management devices, at home or in a lounge. Filter by brand, colour and price.</p>"
        ),
        "ar": (
            "<p>شيشة روسية من Alpha Hookah وUnion وHoob وMaxx Royal وNube وMattpear: سحبة خفيفة وجسم "
            "ستانلس وتصميم عصري.</p>"
            "<p>الشيشة الروسية تُفكّ بسهولة للتنظيف وتتوافق مع مجموعة واسعة من الرؤوس واللي ومنظمات الحرارة، "
            "في المنزل أو في المقهى. صفِّ حسب الماركة واللون والسعر.</p>"
        ),
        "seo_title": "Rus Nargile Takımı Nedir, Nasıl Seçilir?",
        "seo_text": (
            "<p>Rus nargile takımları, son yıllarda nargile kültürüne yön veren modern tasarımlardır. Paslanmaz "
            "çelik ya da alüminyum gövdeleri, hassas işlenmiş parçaları ve hafif çekişleriyle hem bireysel "
            "kullanıcıların hem de kafe işletmelerinin ilk tercihleri arasında yer alırlar.</p>"
            "<h3>Rus nargile takımının öne çıkan özellikleri</h3>"
            "<ul>"
            "<li><strong>Çekiş:</strong> Birçok modelde difüzör ya da ayarlanabilir hava kanalı sayesinde çekişi "
            "ve sesi kendinize göre ayarlayabilirsiniz.</li>"
            "<li><strong>Kolay temizlik:</strong> Gövde parçaları sökülür, şaft ve hava yolları kolayca yıkanır; "
            "böylece aromalar birbirine karışmaz.</li>"
            "<li><strong>Tahliye valfi:</strong> Şişede biriken dumanı tek nefeste boşaltarak her çekişte taze "
            "bir duman almanızı sağlar.</li>"
            "<li><strong>Geniş uyum:</strong> Standart lüle oturağı ve marpuç girişi sayesinde farklı "
            '<a href="/catalog/luleler">lüle</a>, <a href="/catalog/marpuclar">marpuç</a> ve '
            '<a href="/catalog/kozlukler">közlük</a> seçenekleriyle kullanılabilir.</li>'
            "</ul>"
            "<h3>Rus nargile markaları</h3>"
            "<p>Alpha Hookah, Union, Hoob, Maxx Royal, Nube, Mattpear ve Blade gibi markaların her biri kendi "
            "tasarım diline sahiptir: kimi minimal ve hafif, kimi ağır gövdesi ve göz alıcı renkleriyle öne çıkar. "
            "Aynı markanın farklı renk ve boydaki modellerini ürün sayfalarında karşılaştırabilirsiniz.</p>"
            "<h3>Rus nargile takımı seçerken</h3>"
            "<p>Gövdenin yüksekliği, şişenin hacmi ve gövde malzemesi içimin karakterini belirler. Uzun bir şaft "
            "daha serin bir duman verir; mini ve orta boy modeller ise az yer kaplar. Kafede kullanacaksanız "
            "yedek parçası kolay bulunan ve aynı modelden birden fazla tedarik edebileceğiniz markaları tercih "
            'etmenizi öneririz. Bütün modelleri karşılaştırmak için <a href="/catalog/nargile-takimlari">nargile '
            "takımları</a> sayfamıza da göz atabilirsiniz.</p>"
        ),
    },
    "celik-takimlar": {
        "tr": (
            "<p>Paslanmaz çelik gövdeli nargile takımları: paslanmaya dayanıklı, kolay temizlenen ve uzun yıllar "
            "kullanılan Sultan, Legend ve Adalya modelleri.</p>"
            "<p>Çelik nargile takımları, sağlam yapıları ve bakım kolaylıklarıyla hem ev kullanımında hem de "
            "yoğun çalışan kafelerde güvenle tercih edilir.</p>"
        ),
        "en": (
            "<p>Stainless steel hookah sets from Sultan, Legend and Adalya: rust-resistant, easy to clean and "
            "built to last for years.</p>"
            "<p>Sturdy and low-maintenance, steel hookahs are a safe choice for home use and for busy "
            "lounges alike.</p>"
        ),
        "ar": (
            "<p>أطقم شيشة ستانلس ستيل من Sultan وLegend وAdalya: مقاومة للصدأ، سهلة التنظيف وتدوم لسنوات.</p>"
            "<p>شيشة الستانلس متينة وسهلة الصيانة، خيار موثوق للاستخدام المنزلي وللمقاهي المزدحمة.</p>"
        ),
        "seo_title": "Çelik Nargile Takımı Neden Tercih Edilir?",
        "seo_text": (
            "<p>Çelik nargile takımlarının en büyük avantajı dayanıklılıklarıdır. Paslanmaz çelik gövde neme ve "
            "sıcaklığa karşı dirençlidir, koku tutmaz ve yıllar boyunca ilk günkü görünümünü korur. Bu yüzden "
            "özellikle her gün çok sayıda takımın kullanıldığı kafe ve işletmelerde sık tercih edilir.</p>"
            "<h3>Çelik nargile takımı alırken</h3>"
            "<ul>"
            "<li><strong>Gövde:</strong> Paslanmaz çelik gövde, uzun ömür ve kolay temizlik için temel "
            "ölçüttür.</li>"
            "<li><strong>Şişe ve birleşim:</strong> Şişenin gövdeye sıkı oturması sızdırmazlığı ve dengeli çekişi "
            "sağlar.</li>"
            "<li><strong>Boy:</strong> Uzun modeller daha serin bir içim sunar; mini modeller taşıma kolaylığı "
            "sağlar.</li>"
            "</ul>"
            '<p>Takımınızla uyumlu <a href="/catalog/luleler">lüleleri</a>, <a href="/catalog/marpuclar">'
            'marpuçları</a> ve <a href="/catalog/kozlukler">közlükleri</a> aynı siparişte tamamlayabilirsiniz. '
            'Daha modern bir tasarım arıyorsanız <a href="/catalog/rus-takimlar">Rus nargile takımlarına</a> da '
            "göz atın.</p>"
        ),
    },
    "klasik-takimlar": {
        "tr": (
            "<p>Geleneksel Türk nargilesinin işlemeli gövdesi ve geniş cam şişesiyle klasik nargile takımları: "
            "evde, bahçede ve kafede zamansız bir görünüm.</p>"
            "<p>Klasik nargile takımları, nargile kültürünün alışılmış formunu sevenler için. Farklı boy, renk ve "
            "şişe seçenekleri arasından size uygun olanı seçebilirsiniz.</p>"
        ),
        "en": (
            "<p>Classic Turkish hookah sets with an ornate stem and a wide glass base: a timeless look at home, "
            "in the garden or in your café.</p>"
            "<p>For those who love the traditional form of the Turkish hookah, in a choice of sizes, colours "
            "and bases.</p>"
        ),
        "ar": (
            "<p>أطقم شيشة تركية كلاسيكية بجسم مزخرف وقاعدة زجاجية واسعة: إطلالة خالدة في المنزل أو الحديقة أو "
            "المقهى.</p>"
            "<p>لمحبي الشكل التقليدي للشيشة التركية، بخيارات متعددة من الأحجام والألوان والقواعد.</p>"
        ),
        "seo_title": "Klasik Nargile Takımı: Geleneksel Tasarım",
        "seo_text": (
            "<p>Klasik nargile takımı, kuşaklardır bilinen Türk nargilesinin formunu korur: uzun ve işlemeli bir "
            "gövde, geniş bir cam şişe, lüle ve marpuç. Görünümüyle bir dekorasyon parçası da olan bu takımlar, "
            "keyifli sohbetlerin ve misafir ağırlamanın vazgeçilmezidir.</p>"
            "<h3>Klasik takımın bakımı</h3>"
            "<p>Her kullanımdan sonra şişedeki suyu değiştirip gövdeyi ve şişeyi ılık suyla durulamak takımınızın "
            "kokusuz ve uzun ömürlü olmasını sağlar. Contaları ara ara kontrol etmek sızdırmazlığı korur; aşınan "
            'parçaları <a href="/catalog/malzemeler">nargile malzemeleri</a> kategorimizden yenileyebilirsiniz.</p>'
            "<h3>Klasik mi, modern mi?</h3>"
            "<p>Geleneksel bir görünüm ve içim arıyorsanız klasik bir takım doğru tercih. Hafif çekiş, kolay "
            'temizlik ve modern tasarım öncelikliyse <a href="/catalog/rus-takimlar">Rus</a> ya da '
            '<a href="/catalog/celik-takimlar">çelik nargile takımlarını</a> inceleyebilirsiniz.</p>'
        ),
    },
    "masaustu-takimlar": {
        "tr": (
            "<p>Kısa boylu, kompakt ve sağlam duruşlu masaüstü nargile takımları: Legend, Alpha Hookah, Sultan ve "
            "Quasar'ın az yer kaplayan şık modelleri.</p>"
            "<p>Masaüstü nargileler masada ve küçük alanlarda rahatça kullanılmak için tasarlanır. Kısa boylarına "
            "rağmen dengeli bir çekiş sunan modelleri karşılaştırabilirsiniz.</p>"
        ),
        "en": (
            "<p>Short, compact and stable tabletop hookahs: space-saving models from Legend, Alpha Hookah, "
            "Sultan and Quasar.</p>"
            "<p>Made for the table and for small spaces, with a balanced draw despite their height.</p>"
        ),
        "ar": (
            "<p>شيشة طاولة قصيرة ومدمجة وثابتة: موديلات أنيقة لا تأخذ مساحة من Legend وAlpha Hookah وSultan "
            "وQuasar.</p>"
            "<p>مصممة للطاولة والمساحات الصغيرة، بسحبة متوازنة رغم قصرها.</p>"
        ),
        "seo_title": "Masaüstü Nargile Takımı Kimler İçin Uygun?",
        "seo_text": (
            "<p>Masaüstü nargile takımları alçak gövdeleri ve geniş tabanlarıyla masada devrilmeden durur; "
            "balkon, teras ya da küçük salonlar gibi sınırlı alanlarda pratik bir çözüm sunar. Kompakt yapıları "
            "sayesinde taşınmaları ve saklanmaları da kolaydır.</p>"
            "<h3>Masaüstü takım seçerken</h3>"
            "<ul>"
            "<li><strong>Taban:</strong> Geniş ve ağır tabanlı modeller daha sağlam durur.</li>"
            "<li><strong>Çekiş:</strong> İyi tasarlanmış bir hava yolu, kısa gövdeye rağmen rahat ve dengeli bir "
            "çekiş sağlar.</li>"
            "<li><strong>Malzeme:</strong> Gövdenin ve şişenin malzemesi hem görünümü hem de temizlik kolaylığını "
            "belirler.</li>"
            "</ul>"
            '<p>Takımı yanınızda götürmek istiyorsanız <a href="/catalog/cantali-nargile-takimlari">çantalı nargile '
            'takımlarına</a>, bütün seçenekler için <a href="/catalog/nargile-takimlari">nargile takımları</a> '
            "sayfamıza bakabilirsiniz.</p>"
        ),
    },
    "cantali-nargile-takimlari": {
        "tr": (
            "<p>Taşıma çantasıyla gelen nargile takımları: Legend, Sultan ve Oduman modelleri, korunaklı "
            "çantasında güvenle taşınır, düzenli saklanır.</p>"
            "<p>Seyahatte, piknikte ya da kampta nargilenizi yanınızda götürmek istiyorsanız çantalı takımlar tam "
            "size göre: çanta, şişeyi ve gövdeyi darbelere karşı korur.</p>"
        ),
        "en": (
            "<p>Hookah sets that come with a carry case: Legend, Sultan and Oduman models, safe to carry and "
            "neatly stored.</p>"
            "<p>Taking your hookah on a trip, a picnic or camping? The case protects the base and the stem "
            "on the way.</p>"
        ),
        "ar": (
            "<p>أطقم شيشة مع حقيبة حمل: موديلات Legend وSultan وOduman، تُنقل بأمان وتُحفظ بترتيب.</p>"
            "<p>تأخذ شيشتك في سفر أو نزهة أو تخييم؟ الحقيبة تحمي القاعدة والجسم أثناء التنقل.</p>"
        ),
        "seo_title": "Çantalı Nargile Takımı ile Her Yerde Nargile",
        "seo_text": (
            "<p>Çantalı nargile takımları gövde, şişe, lüle ve marpuç gibi bütün parçaları bir çantada bir arada "
            "tutar. Böylece takımınızı kırılma kaygısı olmadan taşıyabilir, kullanmadığınızda da tozdan ve "
            "darbeden koruyarak düzenli şekilde saklayabilirsiniz.</p>"
            "<h3>Çantalı takım alırken</h3>"
            "<ul>"
            "<li><strong>Çanta:</strong> Sert ya da dolgulu, iç bölmeleri olan çantalar şişeyi daha iyi "
            "korur.</li>"
            "<li><strong>Boy:</strong> Sık taşıyacaksanız orta ve kısa boylu takımlar daha pratiktir.</li>"
            "<li><strong>Şişe:</strong> Kalın camdan bir şişe yolculukta darbelere karşı daha dayanıklıdır.</li>"
            "</ul>"
            "<p>Elinizdeki takım için ayrı bir nargile çantası arıyorsanız "
            '<a href="/catalog/malzemeler">nargile malzemeleri</a> kategorimize göz atabilirsiniz.</p>'
        ),
    },
    "luleler": {
        "tr": (
            "<p>Killer, phunnel, toprak, seramik ve cam nargile lüleleri: Kong, Oblako, Solaris, Art Bowl ve "
            "Oduman gibi markaların ısıyı dengeli dağıtan modelleri.</p>"
            "<p>Lüle, nargilenin lezzetini ve duman yoğunluğunu en çok etkileyen parçasıdır. Kullandığınız "
            "közlüğe uygun lüleyi seçerek her seferinde aynı dengeli içimi yakalayabilirsiniz.</p>"
        ),
        "en": (
            "<p>Killer, phunnel, clay, ceramic and glass hookah bowls: models from Kong, Oblako, Solaris, Art "
            "Bowl and Oduman that spread the heat evenly.</p>"
            "<p>The bowl shapes the flavour and the cloud more than any other part. Match it to your heat "
            "management device for the same balanced session every time.</p>"
        ),
        "ar": (
            "<p>رؤوس شيشة Killer وPhunnel وفخار وسيراميك وزجاج: موديلات من Kong وOblako وSolaris وArt Bowl "
            "وOduman توزّع الحرارة بالتساوي.</p>"
            "<p>الرأس هو أكثر جزء يؤثر على النكهة وكثافة الدخان. اختره مناسبًا لمنظم الحرارة لديك لتحصل على "
            "جلسة متوازنة في كل مرة.</p>"
        ),
        "seo_title": "Nargile Lülesi Nasıl Seçilir?",
        "seo_text": (
            "<p>Nargile lülesi ısının karışıma nasıl aktarılacağını belirler; doğru lüle hem lezzeti hem de içim "
            "süresini doğrudan etkiler. Lüle seçerken malzemesine, haznesinin biçimine ve kullandığınız közlükle "
            "uyumuna bakmak gerekir.</p>"
            "<h3>Lüle çeşitleri</h3>"
            "<ul>"
            "<li><strong>Phunnel lüleler:</strong> Ortadaki yükseltilmiş kanal şerbetin aşağı akmasını önler; uzun "
            "ve tutarlı bir içim sunar.</li>"
            "<li><strong>Killer lüleler:</strong> Rus nargile kültüründe yaygın, geniş hazneli ve ısıyı dengeli "
            "dağıtan modellerdir.</li>"
            "<li><strong>Toprak ve seramik lüleler:</strong> Isıyı yavaş ve dengeli tutar, klasik bir içim "
            "sunar.</li>"
            "<li><strong>Cam lüleler:</strong> Aroma tutmaz, kolayca temizlenir.</li>"
            "</ul>"
            "<h3>Lüle ile közlük uyumu</h3>"
            '<p>Isı yönetim cihazı (HMD) kullanıyorsanız lülenizin ağız çapının <a href="/catalog/kozlukler">'
            "közlüğünüzle</a> uyumlu olmasına dikkat edin. Doğru eşleşme kömürün ısısını eşit dağıtır ve yanık "
            'tadı önler. Lülenizi tamamlamak için <a href="/catalog/komurler">nargile kömürlerimize</a> de göz '
            "atabilirsiniz.</p>"
        ),
    },
    "komurler": {
        "tr": (
            "<p>Hindistan cevizinden doğal ve çakmakla yanan nargile kömürleri: One Nation, SultanCoco, Bucoco, "
            "Black Coco's ve Venookah, 1 kg'dan 20 kg koliye.</p>"
            "<p>26'lık, 27'lik ve 28'lik küp seçenekleriyle lülenize ve közlüğünüze uygun kömürü seçebilirsiniz. "
            "Kafe ve işletmeler için toptan koli seçeneklerimiz de var.</p>"
        ),
        "en": (
            "<p>Natural coconut and quick-light hookah charcoal: One Nation, SultanCoco, Bucoco, Black Coco's and "
            "Venookah, from 1 kg packs to 20 kg cases.</p>"
            "<p>Choose 26, 27 or 28 mm cubes to suit your bowl and heat management device; cases for cafés "
            "and lounges at wholesale prices.</p>"
        ),
        "ar": (
            "<p>فحم شيشة طبيعي من جوز الهند وفحم سريع الاشتعال: One Nation وSultanCoco وBucoco وBlack Coco's "
            "وVenookah، من عبوة 1 كغ إلى كرتونة 20 كغ.</p>"
            "<p>اختر مكعبات 26 أو 27 أو 28 ملم حسب الرأس ومنظم الحرارة، مع كراتين بأسعار الجملة للمقاهي.</p>"
        ),
        "seo_title": "Nargile Kömürü Seçimi: Doğal mı, Çakmakla Yanan mı?",
        "seo_text": (
            "<p>Nargile kömürü içimin süresini, ısının dengesini ve lezzeti doğrudan etkiler. Hindistan cevizi "
            "kabuğundan preslenen doğal kömürler uzun süre yanar, az kül bırakır ve kokusu çok azdır. Çakmakla "
            "yanan kömürler ise ocağa ihtiyaç duymadan pratik bir başlangıç sunar.</p>"
            "<h3>Nargile kömürü alırken</h3>"
            "<ul>"
            "<li><strong>Küp boyutu:</strong> 26'lık, 27'lik ve 28'lik küpler en yaygın ölçülerdir; boyutu "
            "lülenize ve közlüğünüze göre seçebilirsiniz.</li>"
            "<li><strong>Yanma süresi ve kül:</strong> İyi bir kömür uzun ve dengeli yanar, az ve açık renkli kül "
            "bırakır.</li>"
            "<li><strong>Paket:</strong> Kafe ve işletmeler için 20 kg'lık koliler birim maliyeti düşürür.</li>"
            "</ul>"
            "<p>Kömürünüzü hızlı ve güvenle yakmak için elektrikli ocak, köz tavası ve maşaları "
            '<a href="/catalog/malzemeler">nargile malzemeleri</a> kategorimizde bulabilirsiniz. Isıyı daha iyi '
            'kontrol etmek isterseniz <a href="/catalog/kozlukler">közlükleri</a> inceleyin.</p>'
        ),
    },
    "kozlukler": {
        "tr": (
            "<p>Közlükler ve ısı yönetim cihazları (HMD): Legend, Maxx Royal, Copper Balance ve Alpha modelleriyle "
            "ısıyı sabit tutan, folyoya gerek bırakmayan çözümler.</p>"
            "<p>Közlük kömürün ısısını lüleye eşit dağıtır, yanık tadını önler ve kömürü rüzgâra karşı korur. "
            "Kapaklı ve kapaksız modelleri lülenizin çapına göre seçebilirsiniz.</p>"
        ),
        "en": (
            "<p>Heat management devices (HMD) from Legend, Maxx Royal, Copper Balance and Alpha: steady heat, "
            "no foil needed.</p>"
            "<p>An HMD spreads the charcoal's heat evenly over the bowl, prevents a burnt taste and shields "
            "the coals from the wind. Choose by your bowl's diameter, with or without a lid.</p>"
        ),
        "ar": (
            "<p>منظمات حرارة الشيشة (كالود) من Legend وMaxx Royal وCopper Balance وAlpha: حرارة ثابتة بدون "
            "ورق ألمنيوم.</p>"
            "<p>يوزّع منظم الحرارة حرارة الفحم على الرأس بالتساوي، ويمنع الطعم المحروق ويحمي الفحم من الهواء. "
            "اختره حسب قطر الرأس، بغطاء أو بدونه.</p>"
        ),
        "seo_title": "Közlük (HMD) Ne İşe Yarar?",
        "seo_text": (
            "<p>Közlük, ya da ısı yönetim cihazı (HMD), kömürü lüleye doğrudan temas ettirmeden ısıyı kontrollü "
            "şekilde aktarır. Böylece folyoya gerek kalmaz, ısı daha dengeli dağılır ve karışım daha geç yanar. "
            "Kapaklı modeller kömürü rüzgâra ve küle karşı da korur; açık havada bu fark hemen hissedilir.</p>"
            "<h3>Közlük seçerken</h3>"
            "<ul>"
            '<li><strong>Lüle uyumu:</strong> Közlüğün taban çapı <a href="/catalog/luleler">lülenizin</a> ağız '
            "çapına uygun olmalıdır.</li>"
            "<li><strong>Malzeme:</strong> Alüminyum hafiftir ve çabuk ısınır; çelik ve bakır modeller ısıyı daha "
            "uzun tutar.</li>"
            "<li><strong>Kömür kapasitesi:</strong> Közlüğün aldığı kömür sayısı içimin süresini ve sıcaklığını "
            "belirler.</li>"
            "</ul>"
            '<p>Közlüğünüze uygun <a href="/catalog/komurler">nargile kömürlerini</a> aynı siparişte '
            "tamamlayabilirsiniz.</p>"
        ),
    },
    "marpuclar": {
        "tr": (
            "<p>Silikon ve deri marpuçlar, silikon hortumlar ve marpuç başlıkları: Naimi, NFX, Legend ve "
            "Steamulation gibi markalarla rahat bir çekiş.</p>"
            "<p>Marpuç, nargilenin her içimde elinize aldığınız parçasıdır. Doğru marpuç hem rahat bir çekiş hem "
            "de kolay temizlik sağlar; takımınıza uygun başlık ve hortumları burada bulabilirsiniz.</p>"
        ),
        "en": (
            "<p>Silicone and leather hookah hoses, silicone tubing and hose handles from Naimi, NFX, Legend and "
            "Steamulation, for a smooth draw.</p>"
            "<p>The hose is the part you hold every session: the right one draws easily and cleans easily. "
            "Find handles and tubing to fit your set.</p>"
        ),
        "ar": (
            "<p>لي شيشة سيليكون وجلد، خراطيم سيليكون ومقابض لي من Naimi وNFX وLegend وSteamulation لسحبة "
            "سلسة.</p>"
            "<p>اللي هو الجزء الذي تمسكه في كل جلسة: الصحيح منه سهل السحب وسهل التنظيف. اعثر على المقابض "
            "والخراطيم المناسبة لطقمك.</p>"
        ),
        "seo_title": "Doğru Nargile Marpucunu Seçmek",
        "seo_text": (
            "<p>Marpuç, dumanın gövdeden size ulaştığı yoldur ve çekişin rahatlığını doğrudan etkiler. Bugün en "
            "çok tercih edilen silikon marpuçlar yıkanabilir, koku tutmaz ve uzun ömürlüdür. Deri marpuçlar ise "
            "klasik görünümleriyle geleneksel takımlara yakışır.</p>"
            "<h3>Marpuç alırken</h3>"
            "<ul>"
            "<li><strong>Hortum:</strong> Silikon hortumlar suyla yıkanabilir ve aromaları birbirine "
            "karıştırmaz.</li>"
            "<li><strong>Başlık:</strong> Marpuç başlığının malzemesi ve ağırlığı hem tutuş rahatlığını hem de "
            "görünümü belirler.</li>"
            "<li><strong>Bağlantı:</strong> Marpucun takımınızın marpuç girişine tam oturması sızdırmazlık için "
            "önemlidir.</li>"
            "</ul>"
            '<p>Paylaşımlı kullanım için kişisel <a href="/catalog/sipsiler">sipsilere</a>, marpucunuzu temiz '
            'tutmak için temizleme fırçalarına <a href="/catalog/malzemeler">nargile malzemeleri</a> '
            "kategorimizden ulaşabilirsiniz.</p>"
        ),
    },
    "sipsiler": {
        "tr": (
            "<p>Askılı, kehribar ve çelik sipsiler, külah ve paket sipsiler: Werkbund, Darkside, Steamulation ve "
            "Sultan gibi markalarla kişisel ve hijyenik kullanım.</p>"
            "<p>Kişisel bir sipsi nargileyi paylaşırken hijyen sağlar ve tarzınızı yansıtır. Boyunda taşınan "
            "askılı modellerden kafeler için paket sipsilere kadar farklı seçenekleri inceleyebilirsiniz.</p>"
        ),
        "en": (
            "<p>Lanyard, amber and steel mouth tips plus disposable tips in packs, from Werkbund, Darkside, "
            "Steamulation and Sultan, for personal, hygienic use.</p>"
            "<p>A mouth tip of your own keeps a shared hookah hygienic and shows your style; packs of "
            "disposable tips for cafés.</p>"
        ),
        "ar": (
            "<p>مباسم شخصية بعلّاقة وكهرمان وستانلس، ومباسم استخدام واحد بالعبوات من Werkbund وDarkside "
            "وSteamulation وSultan، لاستخدام شخصي وصحي.</p>"
            "<p>المبسم الشخصي يحافظ على النظافة عند مشاركة الشيشة ويعكس ذوقك؛ وعبوات مباسم استخدام واحد "
            "للمقاهي.</p>"
        ),
        "seo_title": "Sipsi Nedir, Neden Kullanılır?",
        "seo_text": (
            "<p>Sipsi, marpucun ucuna takılan kişisel ağızlıktır. Kafede ya da arkadaş ortamında nargile "
            "paylaşılırken herkesin kendi sipsisini kullanması hijyen için en pratik çözümdür. Askılı sipsiler "
            "boyunda taşındığı için kaybolmaz ve her an elinizin altındadır.</p>"
            "<h3>Sipsi çeşitleri</h3>"
            "<ul>"
            "<li><strong>Askılı sipsiler:</strong> İple ya da zincirle boyunda taşınan kişisel sipsiler.</li>"
            "<li><strong>Kehribar sipsiler:</strong> Doğal görünümleriyle hem kullanım hem hediye için tercih "
            "edilir.</li>"
            "<li><strong>Çelik sipsiler:</strong> Dayanıklıdır ve kolayca temizlenir.</li>"
            "<li><strong>Külah ve paket sipsiler:</strong> Kafeler ve işletmeler için tek kullanımlık, paket "
            "halinde.</li>"
            "</ul>"
            '<p>Sipsinizin takılacağı <a href="/catalog/marpuclar">marpuçlara</a> da göz atabilirsiniz.</p>'
        ),
    },
    "siseler": {
        "tr": (
            "<p>Yedek nargile şişeleri: Oduman, Legend, Mr.Wood ve Steamulation modelleri, farklı renk, boy ve "
            "bağlantı seçenekleriyle.</p>"
            "<p>Kırılan ya da yenilemek istediğiniz şişeyi takımınıza uygun modeller arasından seçebilir, "
            "nargilenize yeni bir görünüm kazandırabilirsiniz.</p>"
        ),
        "en": (
            "<p>Replacement hookah bases from Oduman, Legend, Mr.Wood and Steamulation, in a choice of colours, "
            "sizes and fittings.</p>"
            "<p>Replace a broken base or give your hookah a new look with one that fits your set.</p>"
        ),
        "ar": (
            "<p>قواعد شيشة بديلة من Oduman وLegend وMr.Wood وSteamulation، بألوان وأحجام ووصلات مختلفة.</p>"
            "<p>استبدل قاعدة مكسورة أو امنح شيشتك إطلالة جديدة بقاعدة تناسب طقمك.</p>"
        ),
        "seo_title": "Nargile Şişesi Seçerken Uyuma Dikkat",
        "seo_text": (
            "<p>Nargile şişesi takımın hem görünümünü hem de dengesini belirler. Yedek şişe alırken en önemli "
            "ölçüt, şişenin gövdenizle uyumlu olmasıdır.</p>"
            "<ul>"
            "<li><strong>Bağlantı:</strong> Şişenin ağız ölçüsü ve bağlantı türü (geçme ya da vidalı) gövdenize "
            "uymalıdır.</li>"
            "<li><strong>Cam kalınlığı:</strong> Kalın camdan şişeler darbelere karşı daha dayanıklıdır.</li>"
            "<li><strong>Boy:</strong> Büyük boy şişeler takıma sağlam bir duruş kazandırır; küçük boy şişeler "
            "kompakt takımlara yakışır.</li>"
            "</ul>"
            "<p>Şişeyle birlikte değiştirmek isteyebileceğiniz conta, difüzör ve temizleme fırçalarını "
            '<a href="/catalog/malzemeler">nargile malzemeleri</a> kategorimizde bulabilirsiniz.</p>'
        ),
    },
    "malzemeler": {
        "tr": (
            "<p>Nargile malzemeleri ve yedek parçalar: maşa, folyo, conta, difüzör, rüzgarlık, köz tavası, "
            "elektrikli ocak, nargile çantası ve temizleme fırçaları.</p>"
            "<p>Takımınızın bakımı ve günlük kullanımı için gereken her şey bir arada. Kafe ve işletmeler için "
            "sık tükenen malzemelerde toptan fiyatlarımız var.</p>"
        ),
        "en": (
            "<p>Hookah accessories and spare parts: tongs, foil, grommets, diffusers, wind covers, charcoal "
            "pans, electric burners, hookah bags and cleaning brushes.</p>"
            "<p>Everything your set needs for daily use and upkeep, with wholesale prices on everyday "
            "supplies for cafés.</p>"
        ),
        "ar": (
            "<p>مستلزمات وقطع غيار الشيشة: ملاقط، ورق ألمنيوم، جلدات، ناشرات، أغطية هواء، صحون فحم، مواقد "
            "كهربائية، حقائب شيشة وفُرش تنظيف.</p>"
            "<p>كل ما يحتاجه طقمك للاستخدام اليومي والعناية، بأسعار جملة للمستلزمات اليومية للمقاهي.</p>"
        ),
        "seo_title": "Nargile Malzemeleri ve Yedek Parçalar",
        "seo_text": (
            "<p>İyi bir nargile takımı, doğru malzemelerle uzun yıllar ilk günkü gibi çalışır. Bu kategoride "
            "takımınızın bakımı, kömürün yakılması ve günlük kullanım için gereken malzemeleri bir araya "
            "getirdik.</p>"
            "<h3>Neler bulabilirsiniz?</h3>"
            "<ul>"
            "<li><strong>Kömür hazırlığı:</strong> Elektrikli ocaklar, köz tavaları, maşalar, pürmüzler ve "
            "gazlar.</li>"
            "<li><strong>Lüle hazırlığı:</strong> Folyolar, folyo delici iğneler ve rüzgarlıklar.</li>"
            "<li><strong>Yedek parçalar:</strong> Contalar, difüzörler, adaptörler, rezistanslar, bez bantlar ve "
            "yapıştırıcılar.</li>"
            "<li><strong>Bakım ve taşıma:</strong> Şişe ve marpuç temizleme fırçaları, nargile çantaları.</li>"
            "</ul>"
            '<p>Isıyı folyosuz kontrol etmek için <a href="/catalog/kozlukler">közlüklere</a>, yeni bir takım '
            'arıyorsanız <a href="/catalog/nargile-takimlari">nargile takımlarına</a> göz atabilirsiniz.</p>'
        ),
    },
    # Introductions only: a brand's own page (/marka) and its own copy say the rest.
    "mr-wood": {
        "tr": "<p>Mr.Wood'un Classic Adonis ve Classic Artemis nargile takımları: kristal ve siyah şişe seçenekleriyle, marpuç dahil.</p>",
        "en": "<p>Mr.Wood's Classic Adonis and Classic Artemis hookah sets, with a crystal or black base and the hose included.</p>",
        "ar": "<p>أطقم شيشة Classic Adonis وClassic Artemis من Mr.Wood، بقاعدة كريستال أو سوداء مع اللي.</p>",
    },
    "steamulation": {
        "tr": "<p>Steamulation nargile takımları: Pure, Pure One ve Ultimate serileri, Alman mühendisliği ve şık tasarımıyla; orijinal yedek parça ve aksesuarlarıyla.</p>",
        "en": "<p>Steamulation hookahs: the Pure, Pure One and Ultimate series, German engineering and sleek design, with original spare parts and accessories.</p>",
        "ar": "<p>شيشة Steamulation: سلاسل Pure وPure One وUltimate، هندسة ألمانية وتصميم أنيق، مع قطع غيار وإكسسوارات أصلية.</p>",
    },
    "meduse": {
        "tr": "<p>Meduse Experience nargileleri: Aurita, Cascada, Coralica ve Elementa gibi heykel gibi tasarımlarıyla koleksiyonluk premium nargile takımları.</p>",
        "en": "<p>Meduse Experience hookahs: Aurita, Cascada, Coralica, Elementa and more, sculptural premium hookahs worth collecting.</p>",
        "ar": "<p>شيشة Meduse Experience: Aurita وCascada وCoralica وElementa وغيرها، شيشة فاخرة بتصاميم أشبه بالمنحوتات.</p>",
    },
    "quasar": {
        "tr": "<p>Quasar Arguile nargile takımları ile Raas lüleler, LED taban ve tripod ayak gibi orijinal Quasar parçaları.</p>",
        "en": "<p>Quasar Arguile hookahs and original Quasar parts: Raas bowls, the LED base and the tripod stand.</p>",
        "ar": "<p>شيشة Quasar Arguile وقطع Quasar الأصلية: رؤوس Raas وقاعدة LED وحامل ثلاثي.</p>",
    },
    "panada": {
        "tr": "<p>Panada Hookah'ın premium nargile takımı: modern tasarımı ve özenli işçiliğiyle.</p>",
        "en": "<p>Panada Hookah's premium hookah set: modern design, careful craftsmanship.</p>",
        "ar": "<p>طقم شيشة Panada Hookah الفاخر: تصميم عصري وصناعة متقنة.</p>",
    },
    "oyun-eglence": {
        "tr": "<p>Tavla, okey takımı ve iskambil kağıtları: sedefli ve masif tavlalar, ahşap ve çantalı okey takımları; ev ve kafeler için.</p>",
        "en": "<p>Backgammon boards, okey sets and playing cards: mother-of-pearl and solid wood boards, wooden okey sets with cases, for home and cafés.</p>",
        "ar": "<p>طاولة زهر، أطقم أوكي وورق لعب: طاولات صدف وخشب صلب، أطقم أوكي خشبية مع حقيبة، للمنزل والمقاهي.</p>",
    },
}


# The storefront's new default texts (saas_frontend lib/homepage.ts DEFAULT_SEO_INTRO,
# lib/home.ts FALLBACK_SLIDE), word for word: the homepage's translation
# memory looks a text up exactly as written, so without these the English and
# Arabic pages would show them in Turkish.
MEMORY: dict[str, dict[str, str]] = {
    "Nargile Takımı ve Nargile Malzemelerinde Aradığınız Her Şey": {
        "en": "Hookah Sets and Hookah Supplies: Everything You Need",
        "ar": "أطقم الشيشة ومستلزماتها: كل ما تبحث عنه",
    },
    (
        "Nargile Malzemesi; Rus, çelik, klasik, masaüstü ve çantalı nargile takımlarından lüle, nargile kömürü, "
        "közlük, marpuç ve sipsiye kadar nargileye dair her şeyi bir arada sunar. Sultan Nargile, Legend Hookah, "
        "Oduman, Alpha Hookah, Steamulation ve Meduse gibi markaların orijinal ürünlerini karşılaştırıp size en "
        "uygun olanı seçebilirsiniz."
    ): {
        "en": (
            "Nargile Malzemesi brings together everything for the hookah, from Russian-style, stainless steel, "
            "classic, tabletop and travel hookah sets to bowls, charcoal, heat management devices, hoses and mouth "
            "tips. Compare original products from Sultan Nargile, Legend Hookah, Oduman, Alpha Hookah, Steamulation "
            "and Meduse and choose the one that suits you."
        ),
        "ar": (
            "تجمع Nargile Malzemesi كل ما يخص الشيشة في مكان واحد، من الشيشة الروسية والستانلس ستيل والكلاسيكية "
            "وشيشة الطاولة والشيشة مع حقيبة، إلى الرؤوس والفحم ومنظمات الحرارة واللي والمباسم. قارن المنتجات "
            "الأصلية من Sultan Nargile وLegend Hookah وOduman وAlpha Hookah وSteamulation وMeduse واختر ما يناسبك."
        ),
    },
    (
        "Evinde keyifle nargile içenler için de, her gün onlarca takımın çalıştığı kafeler için de doğru ürünü "
        "bulmak kolay olmalı. Bu yüzden ürünlerimizi türlerine ve markalarına göre ayırıyor, her kategoride seçim "
        "yaparken işinize yarayacak bilgileri paylaşıyoruz. Kafe, toptancı ve bayilere özel fiyatlar, hızlı kargo "
        "ve orijinal ürün güvencesiyle ihtiyacınız olan her şey kapınızda."
    ): {
        "en": (
            "Finding the right product should be easy, whether you enjoy a hookah at home or run a lounge with "
            "dozens of sets working every day. That is why we sort our products by type and brand and share, in "
            "every category, what helps you choose. Prices for cafés, wholesalers and dealers, fast shipping and a "
            "genuine-product guarantee bring everything you need to your door."
        ),
        "ar": (
            "يجب أن يكون العثور على المنتج المناسب سهلًا، سواء كنت تستمتع بالشيشة في المنزل أو تدير مقهى تعمل فيه "
            "عشرات الأطقم يوميًا. لذلك نصنّف منتجاتنا حسب النوع والماركة، ونشارك في كل فئة ما يساعدك على الاختيار. "
            "أسعار خاصة للمقاهي وتجار الجملة والموزعين، وشحن سريع وضمان المنتج الأصلي، ليصلك كل ما تحتاجه إلى باب "
            "منزلك."
        ),
    },
    "Nargile Takımları, Lüleler ve Premium Sarf Malzemeleri.": {
        "en": "Hookah Sets, Bowls and Premium Supplies.",
        "ar": "أطقم شيشة ورؤوس ومستلزمات فاخرة.",
    },
}


def _translations(raw) -> dict:
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _nav_has_hub(items: list) -> bool:
    return any(isinstance(i, dict) and i.get("slug") == HUB for i in items)


def upgrade() -> None:
    conn = op.get_bind()

    # 1. The hub: a live address again, nobody's old slug.
    conn.execute(sa.text("DELETE FROM slug_redirects WHERE kind = 'category' AND old_slug = :s"), {"s": HUB})
    hub = conn.execute(sa.text("SELECT id FROM categories WHERE slug = :s"), {"s": HUB}).scalar()
    if hub is None:
        hub = conn.execute(
            sa.text(
                "INSERT INTO categories (name, slug, parent_id, sort_order, is_active) "
                "VALUES (:name, :slug, NULL, 0, true) RETURNING id"
            ),
            {"name": HUB_NAME["tr"], "slug": HUB},
        ).scalar_one()
    else:
        # One the admin switched off or filed under another: a parent, and shown.
        conn.execute(sa.text("UPDATE categories SET is_active = true, parent_id = NULL WHERE id = :id"), {"id": hub})

    # 2. The takım categories under it — those still at the top, with no
    #    children of their own (the tree is two levels deep). Their order
    #    among themselves stays the admin's.
    conn.execute(
        sa.text(
            "UPDATE categories c SET parent_id = :hub "
            "WHERE c.slug = :slug AND c.parent_id IS NULL AND c.id <> :hub "
            "AND NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = c.id)"
        ),
        [{"hub": hub, "slug": slug} for slug in CHILDREN],
    )

    # 3. Each category's words, where it has none.
    for slug, copy in COPY.items():
        row = conn.execute(
            sa.text("SELECT id, description, seo_title, seo_text, translations FROM categories WHERE slug = :s"),
            {"s": slug},
        ).mappings().first()
        if row is None:
            continue
        changes: dict = {}
        if not (row["description"] or "").strip():
            changes["description"] = copy["tr"]
        if "seo_text" in copy and not (row["seo_title"] or "").strip() and not (row["seo_text"] or "").strip():
            changes["seo_title"], changes["seo_text"] = copy["seo_title"], copy["seo_text"]
        translations = _translations(row["translations"])
        touched = False
        for lang in ("en", "ar"):
            entry = dict(translations.get(lang) or {})
            if slug == HUB and not entry.get("name"):
                entry["name"] = HUB_NAME[lang]
            # A translation is only shown with its name; without one the
            # description would never be read.
            if entry.get("name") and not (entry.get("description") or "").strip():
                entry["description"] = copy[lang]
                translations[lang] = entry
                touched = True
        if touched:
            changes["translations"] = json.dumps(translations, ensure_ascii=False)
        if not changes:
            continue
        sets = ", ".join(
            f"{k} = CAST(:{k} AS JSONB)" if k == "translations" else f"{k} = :{k}" for k in changes
        )
        conn.execute(sa.text(f"UPDATE categories SET {sets} WHERE id = :id"), {**changes, "id": row["id"]})

    # 4. The English and Arabic of the storefront's new default texts, where
    #    the memory has none of its own.
    row = conn.execute(sa.text("SELECT data FROM site_content WHERE key = 'homepage_i18n'")).first()
    memory = _translations(row[0]) if row else {}
    added = False
    for source, langs in MEMORY.items():
        for lang, text in langs.items():
            entries = memory.setdefault(lang, {})
            if not entries.get(source):
                entries[source] = text
                added = True
    if added:
        conn.execute(
            sa.text(
                "INSERT INTO site_content (key, data, updated_at) VALUES ('homepage_i18n', CAST(:d AS JSONB), now()) "
                "ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()"
            ),
            {"d": json.dumps(memory, ensure_ascii=False)},
        )

    # 5. The menus: "Nargile Takımları" after "Fırsat Ürünleri" in the top bar,
    #    and at the head of the category column. Only a saved menu is changed:
    #    the storefront's default already has the entry.
    raw = conn.execute(sa.text("SELECT data FROM site_content WHERE key = 'homepage'")).scalar()
    doc = _translations(raw)
    nav = doc.get("navigation")
    if isinstance(nav, dict):
        changed = False
        top = nav.get("top")
        if isinstance(top, list) and not _nav_has_hub(top) and len(top) < NAV_TOP_MAX:
            deals = next((i for i, e in enumerate(top) if isinstance(e, dict) and e.get("href") == "/firsat-urunleri"), -1)
            top.insert(deals + 1, dict(NAV_ENTRY))
            changed = True
        sidebar = nav.get("sidebar")
        if isinstance(sidebar, list) and not _nav_has_hub(sidebar) and len(sidebar) < NAV_SIDEBAR_MAX:
            sidebar.insert(0, dict(NAV_ENTRY))
            changed = True
        if changed:
            conn.execute(
                sa.text("UPDATE site_content SET data = CAST(:d AS JSONB), updated_at = now() WHERE key = 'homepage'"),
                {"d": json.dumps(doc, ensure_ascii=False)},
            )


def downgrade() -> None:
    conn = op.get_bind()
    hub = conn.execute(sa.text("SELECT id FROM categories WHERE slug = :s"), {"s": HUB}).scalar()

    raw = conn.execute(sa.text("SELECT data FROM site_content WHERE key = 'homepage'")).scalar()
    doc = _translations(raw)
    nav = doc.get("navigation")
    if isinstance(nav, dict):
        for key in ("top", "sidebar"):
            if isinstance(nav.get(key), list):
                nav[key] = [e for e in nav[key] if e != NAV_ENTRY]
        conn.execute(
            sa.text("UPDATE site_content SET data = CAST(:d AS JSONB) WHERE key = 'homepage'"),
            {"d": json.dumps(doc, ensure_ascii=False)},
        )

    row = conn.execute(sa.text("SELECT data FROM site_content WHERE key = 'homepage_i18n'")).first()
    if row:
        memory = _translations(row[0])
        for source, langs in MEMORY.items():
            for lang, text in langs.items():
                if (memory.get(lang) or {}).get(source) == text:
                    memory[lang].pop(source)
        conn.execute(
            sa.text("UPDATE site_content SET data = CAST(:d AS JSONB) WHERE key = 'homepage_i18n'"),
            {"d": json.dumps(memory, ensure_ascii=False)},
        )

    for slug, copy in COPY.items():
        row = conn.execute(
            sa.text("SELECT id, description, seo_title, seo_text, translations FROM categories WHERE slug = :s"),
            {"s": slug},
        ).mappings().first()
        if row is None:
            continue
        changes: dict = {}
        if row["description"] == copy["tr"]:
            changes["description"] = None
        if "seo_text" in copy and row["seo_title"] == copy["seo_title"] and row["seo_text"] == copy["seo_text"]:
            changes["seo_title"] = changes["seo_text"] = None
        translations = _translations(row["translations"])
        touched = False
        for lang in ("en", "ar"):
            entry = translations.get(lang)
            if isinstance(entry, dict) and entry.get("description") == copy[lang]:
                entry.pop("description")
                touched = True
        if touched:
            changes["translations"] = json.dumps(translations, ensure_ascii=False)
        if changes:
            sets = ", ".join(
                f"{k} = CAST(:{k} AS JSONB)" if k == "translations" else f"{k} = :{k}" for k in changes
            )
            conn.execute(sa.text(f"UPDATE categories SET {sets} WHERE id = :id"), {**changes, "id": row["id"]})

    if hub is None:
        return
    conn.execute(
        sa.text("UPDATE categories SET parent_id = NULL WHERE parent_id = :hub AND slug = ANY(:kids)"),
        {"hub": hub, "kids": CHILDREN},
    )
    # The hub goes only if nothing else hangs off it.
    in_use = conn.execute(
        sa.text(
            "SELECT 1 FROM categories WHERE parent_id = :hub "
            "UNION ALL SELECT 1 FROM products WHERE category_id = :hub "
            "UNION ALL SELECT 1 FROM product_categories WHERE category_id = :hub LIMIT 1"
        ),
        {"hub": hub},
    ).scalar()
    if not in_use:
        conn.execute(sa.text("DELETE FROM category_product_orders WHERE category_id = :hub"), {"hub": hub})
        conn.execute(sa.text("DELETE FROM categories WHERE id = :hub"), {"hub": hub})
