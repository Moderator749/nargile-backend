"""The shop's legal texts, as first written.

These are the rows the ``legal_documents`` table is seeded with, and the
fallback when a row is missing, so checkout always has something to show. An
admin edits them from the panel afterwards (``/admin/settings/legal``); nothing
here is read again once a row exists.

The two agreements are drafts with the shop's own details left in brackets: they
are templates to be completed and reviewed, not legal advice.
"""

SHOP_NAME = "Sultan Nargile"

# The framed notice beside the payment step, which the blueprint fixes word for
# word. Everything else below is a draft.
PRIVACY_NOTICE_BODY = (
    "Bu siteye üye olurken verdiğiniz ad, soyad, telefon, adres ve e-mail adresiniz başka kişi "
    "yada kuruluşlarla paylaşılmaz. Bu bilgiler sadece Sultan Nargile tarafından sizinle iletişim "
    "için kullanılır. Alışveriş esnasında sepette kredi kartı bilgilerinizi kaydedebilirsiniz. "
    "Yapılan bu kayıt tarafımızdan görülmez, sistem kendi içine kayıt eder."
)

PRIVACY_BODY = """## Veri sorumlusu

Kişisel verileriniz, 6698 sayılı Kişisel Verilerin Korunması Kanunu (KVKK) kapsamında veri sorumlusu olan [Firma unvanı] ([Adres]) tarafından aşağıdaki esaslara göre işlenir.

## İşlenen veriler ve amaçları

Üyelik ve sipariş süreçlerinde ad soyad, telefon, e-posta, teslimat ve fatura adresi, kurumsal müşterilerde vergi dairesi ve vergi numarası bilgilerinizi işliyoruz. Bu veriler yalnızca siparişinizin oluşturulması, kargoya verilmesi, faturalandırılması, iade ve destek taleplerinizin karşılanması ve yasal saklama yükümlülüklerimizin yerine getirilmesi için kullanılır.

## Aktarım

Bilgileriniz başka kişi ya da kuruluşlarla pazarlama amacıyla paylaşılmaz. Teslimatın yapılabilmesi için ad, adres ve telefon bilgileriniz anlaşmalı kargo firmasına; faturalandırma için mali müşavirimize ve yasal olarak yetkili kamu kurumlarına talep hâlinde aktarılır.

## Kredi kartı bilgileri

Kredi kartı bilgileriniz tarafımızca görülmez ve saklanmaz. Ödeme, anlaşmalı ödeme kuruluşunun altyapısı üzerinden alınır; kart saklama tercihini işaretlemeniz hâlinde kart bilgileri yalnızca ödeme kuruluşunun sisteminde saklanır.

## Haklarınız

KVKK'nın 11. maddesi uyarınca verilerinizin işlenip işlenmediğini öğrenme, düzeltilmesini, silinmesini veya anonimleştirilmesini isteme haklarına sahipsiniz. Taleplerinizi [E-posta] adresine iletebilirsiniz; başvurularınız en geç 30 gün içinde yanıtlanır."""

DISTANCE_SALES_BODY = """## 1. Taraflar

SATICI: [Firma unvanı] · Adres: [Adres] · Telefon: [Telefon] · E-posta: [E-posta]
ALICI: Sipariş sırasında belirtilen ad, adres ve iletişim bilgilerinin sahibi.

## 2. Konu

İşbu sözleşmenin konusu, ALICI'nın SATICI'ya ait internet sitesi üzerinden elektronik ortamda siparişini verdiği, nitelikleri ve satış fiyatı sipariş özetinde belirtilen ürünlerin satışı ve teslimi ile ilgili olarak 6502 sayılı Tüketicinin Korunması Hakkında Kanun ve Mesafeli Sözleşmeler Yönetmeliği hükümleri gereğince tarafların hak ve yükümlülüklerinin belirlenmesidir.

## 3. Sözleşme konusu ürün ve ödeme

Ürünlerin cinsi, adedi, KDV dâhil satış fiyatı ve ödeme şekli, sipariş özetinde gösterildiği gibidir. Fiyatlara KDV dâhildir. Gönderim ücreti sipariş tutarına eklenmez; ücretsiz kargo koşulları sağlanmadığında gönderim ücreti teslimat sırasında kargo görevlisine ALICI tarafından ödenir.

## 4. Teslimat

Ürün, ALICI'nın sipariş sırasında belirttiği adrese veya seçtiği kargo şubesine teslim edilir. Teslimat süresi, siparişin onaylanmasından itibaren en geç 30 gündür. ALICI dışında bir kişinin teslim alacağı belirtilmişse, ürün bu kişiye teslim edilir.

## 5. Cayma hakkı

ALICI, ürünü teslim aldığı tarihten itibaren 14 gün içinde hiçbir gerekçe göstermeksizin ve cezai şart ödemeksizin sözleşmeden cayma hakkına sahiptir. Cayma bildirimi [E-posta] adresine veya [Telefon] numarasına iletilir. Cayma hakkının kullanılması hâlinde ürünün kutusu, ambalajı ve varsa standart aksesuarları ile birlikte eksiksiz ve hasarsız olarak iade edilmesi gerekir. İade masrafı SATICI'ya aittir.

Niteliği itibarıyla iade edilemeyen ürünlerde, ambalajı açılmış hijyenik ürünlerde (ağızlık, hortum, kömür gibi) ve ALICI'nın isteği üzerine kişiye özel hazırlanan ürünlerde cayma hakkı kullanılamaz.

## 6. Uyuşmazlıklar

İşbu sözleşmeden doğan uyuşmazlıklarda, Ticaret Bakanlığı'nca ilan edilen parasal sınırlar dâhilinde ALICI'nın yerleşim yerindeki Tüketici Hakem Heyetleri, bu sınırın üzerindeki uyuşmazlıklarda Tüketici Mahkemeleri yetkilidir.

ALICI, siparişi onayladığında işbu sözleşmenin tüm koşullarını kabul etmiş sayılır."""

TERMS_BODY = """## Siteyi kullanma koşulları

Bu siteyi kullanarak, aşağıdaki kuralları kabul etmiş olursunuz. Sitede yer alan metin, görsel ve ürün fotoğraflarının tamamı [Firma unvanı]'na aittir; izinsiz kopyalanamaz ve başka bir mecrada yayımlanamaz.

## Üyelik

Üyelik bilgilerinizin doğru ve güncel olmasından siz sorumlusunuz. Hesabınızın ve şifrenizin güvenliği sizin sorumluluğunuzdadır; hesabınızla verilen siparişler size ait kabul edilir. Gerçeğe aykırı bilgiyle açılan hesaplar kapatılabilir.

## Yaş sınırı ve kullanım

Sitede satılan nargile ve tütün ürünleriyle ilgili malzemeler 18 yaşından küçüklere satılmaz. Siparişinizi vererek 18 yaşından büyük olduğunuzu beyan etmiş olursunuz. Ürünler kullanım amacına uygun kullanılmalıdır; hatalı kullanımdan doğan zararlardan SATICI sorumlu tutulamaz.

## Fiyatlar ve stok

Fiyatlara KDV dâhildir. Fiyatlar ve kampanyalar önceden haber verilmeksizin değiştirilebilir; sipariş anında sepette gösterilen fiyat geçerlidir. Stokta bulunmayan bir ürünün siparişi iptal edilir ve ödeme iade edilir.

## Kurumsal (B2B) hesaplar

Kafe, restoran ve tütün mamulü satış noktaları için açılan hesaplarda fiyatlar, onaylanan fiyat listesine göre hesaplanır. Bu hesaplarda bireysel müşterilere tanımlı ParaPuan, ciro ve havale indirimleri uygulanmaz ve ücretsiz kargo koşulları geçerli değildir.

## İletişim

Soru ve talepleriniz için: [E-posta] · [Telefon]"""

# key -> (title, body). The order is the order the panel lists them in.
DEFAULT_LEGAL: dict[str, tuple[str, str]] = {
    "PRIVACY_NOTICE": ("Sepet Gizlilik Bilgilendirmesi", PRIVACY_NOTICE_BODY),
    "PRIVACY": ("Gizlilik Kuralları", PRIVACY_BODY),
    "DISTANCE_SALES": ("Mesafeli Satış Sözleşmesi", DISTANCE_SALES_BODY),
    "TERMS": ("Şartlar ve Kurallar Sözleşmesi", TERMS_BODY),
}
