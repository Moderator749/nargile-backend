import re

from pydantic import BaseModel, Field, field_validator

# The 81 provinces, as customers write them.
TR_PROVINCES = (
    "Adana", "Adıyaman", "Afyonkarahisar", "Ağrı", "Aksaray", "Amasya", "Ankara", "Antalya", "Ardahan",
    "Artvin", "Aydın", "Balıkesir", "Bartın", "Batman", "Bayburt", "Bilecik", "Bingöl", "Bitlis", "Bolu",
    "Burdur", "Bursa", "Çanakkale", "Çankırı", "Çorum", "Denizli", "Diyarbakır", "Düzce", "Edirne", "Elazığ",
    "Erzincan", "Erzurum", "Eskişehir", "Gaziantep", "Giresun", "Gümüşhane", "Hakkari", "Hatay", "Iğdır",
    "Isparta", "İstanbul", "İzmir", "Kahramanmaraş", "Karabük", "Karaman", "Kars", "Kastamonu", "Kayseri",
    "Kilis", "Kırıkkale", "Kırklareli", "Kırşehir", "Kocaeli", "Konya", "Kütahya", "Malatya", "Manisa",
    "Mardin", "Mersin", "Muğla", "Muş", "Nevşehir", "Niğde", "Ordu", "Osmaniye", "Rize", "Sakarya", "Samsun",
    "Şanlıurfa", "Siirt", "Sinop", "Şırnak", "Sivas", "Tekirdağ", "Tokat", "Trabzon", "Tunceli", "Uşak",
    "Van", "Yalova", "Yozgat", "Zonguldak",
)
ISTANBUL = "İstanbul"

_TR_LOWER = str.maketrans("İIŞĞÜÖÇ", "iışğüöç")


def _key(name: str) -> str:
    return " ".join(name.translate(_TR_LOWER).lower().split())


_BY_KEY = {_key(p): p for p in TR_PROVINCES} | {"istanbul": ISTANBUL, "izmir": "İzmir"}
_PHONE = re.compile(r"^\+?[0-9 ()-]{10,20}$")


class ShippingAddress(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(max_length=20)
    city: str = Field(max_length=40)
    district: str = Field(min_length=2, max_length=80)
    line: str = Field(min_length=5, max_length=300)

    @field_validator("full_name", "district", "line")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        v = v.strip()
        if not _PHONE.match(v) or len(re.sub(r"\D", "", v)) < 10:
            raise ValueError("Enter a valid phone number")
        return v

    @field_validator("city")
    @classmethod
    def _province(cls, v: str) -> str:
        """Canonical province name ("istanbul" → "İstanbul")."""
        try:
            return _BY_KEY[_key(v)]
        except KeyError:
            raise ValueError("Unknown province") from None
