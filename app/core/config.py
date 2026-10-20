from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    # Sessions slide: each visit renews the token (POST /auth/refresh), so a
    # session ends only after this long without one. The panel's staff get the
    # short window — they can change prices, orders and refunds — customers the
    # long one, B2B accounts included.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # admins / staff
    CUSTOMER_SESSION_DAYS: int = 30
    DEBUG: bool = False
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    # Origins matched by pattern on top of CORS_ORIGINS — the storefront's
    # Vercel preview deployments, whose host changes with every deploy.
    # None = the list above only.
    CORS_ORIGIN_REGEX: str | None = None
    # Media URLs written while the API ran on this machine
    # (http://127.0.0.1:8000/media/…) are answered with the address the request
    # actually came in on, so the API works behind a tunnel or a proxy without
    # rewriting the database. A request to the local address itself is untouched.
    REWRITE_LOCAL_MEDIA_URLS: bool = True
    LOCAL_MEDIA_ORIGINS: list[str] = ["http://127.0.0.1:8000", "http://localhost:8000"]
    # Where browsers load /media from when that is not the address requests come
    # in on — behind the storefront's proxy, "https://<storefront>/api/backend/media"
    # (the request's own address is the API's, over plain http: mixed content on
    # an HTTPS page). Uploads are answered with it, and so are stored local media
    # URLs, from any origin. None = each request's own address.
    PUBLIC_MEDIA_URL: str | None = None
    # Uploaded files (brand logos), served at /media.
    MEDIA_ROOT: Path = Path(__file__).resolve().parents[2] / "media"

    # ── Transactional e-mail ────────────────────────────────────────────────
    # With no SMTP_HOST the mailer logs the rendered message instead of sending
    # it, so the shipping notice is visible in development without a mail server.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_STARTTLS: bool = True
    SMTP_TIMEOUT: int = 15
    MAIL_FROM: str = "siparis@nargilemalzemesi.com"
    MAIL_FROM_NAME: str = "Nargile Malzemesi"
    SHOP_NAME: str = "Nargile Malzemesi"
    SHOP_URL: str = "https://nargilemalzemesi.com"
    SHOP_SUPPORT_PHONE: str = "0850 000 00 00"
    # The category every "Fırsat Ürünü" is listed under while its flag is on
    # (kept in step by the product sheet's save). No such category: no sync.
    DEALS_CATEGORY_SLUG: str = "kampanyalar"

    # ── Communication (e-mail / SMS drivers) ────────────────────────────────
    # "auto": SMTP when SMTP_HOST is set, otherwise the mock driver, which logs
    # the message and records it as sent by "mock" — nothing leaves the machine.
    # "resend" needs RESEND_API_KEY. A driver whose keys are missing falls back
    # to the mock, so a half-configured .env never breaks a request.
    EMAIL_DRIVER: str = "auto"  # auto | smtp | resend | mock
    RESEND_API_KEY: str | None = None
    # "mock" logs SMS instead of sending. The gateways need their own keys.
    SMS_DRIVER: str = "mock"  # mock | netgsm | iletimerkezi | webhook
    SMS_SENDER: str = ""  # approved sender name (başlık), e.g. "NARGILEMLZ"
    NETGSM_USERCODE: str | None = None
    NETGSM_PASSWORD: str | None = None
    ILETIMERKEZI_API_KEY: str | None = None
    ILETIMERKEZI_API_HASH: str | None = None
    SMS_WEBHOOK_URL: str | None = None
    SMS_WEBHOOK_TOKEN: str | None = None
    SMS_TIMEOUT: int = 15
    # Appended to every campaign SMS: the opt-out the ETK/İYS rules require,
    # e.g. "Ret icin RET yazip 3XXX'e gonderin. B001". Blank: nothing added.
    SMS_OPT_OUT_TEXT: str = ""

    # ── Password reset ("Şifremi unuttum") ──────────────────────────────────
    # The link in the e-mail; {token} is filled in.
    PASSWORD_RESET_URL: str = "http://localhost:3000/sifre-yenile?token={token}"
    PASSWORD_RESET_TTL_MINUTES: int = 30
    # Requests per account within PASSWORD_RESET_WINDOW_MINUTES, and wrong
    # codes per SMS code, before it is refused.
    PASSWORD_RESET_MAX_REQUESTS: int = 3
    PASSWORD_RESET_WINDOW_MINUTES: int = 15
    PASSWORD_RESET_MAX_ATTEMPTS: int = 5

    # ── PayTR (card payments, iFrame API) ───────────────────────────────────
    # The key and salt are secrets from the PayTR merchant panel ("Bilgi"
    # sayfası); they belong in .env only. Until both are set, card payment is
    # switched off: the token endpoint answers 503 and checkout says so.
    PAYTR_MERCHANT_ID: str = ""
    PAYTR_MERCHANT_KEY: str = ""
    PAYTR_MERCHANT_SALT: str = ""
    # True while testing: PayTR takes test cards and charges nothing.
    PAYTR_TEST_MODE: bool = True
    # Where the customer lands after paying; `{order_id}` is filled in. These
    # are only where the browser goes — the order is paid by the notification
    # PayTR posts to /api/v1/payments/paytr/callback, never by these pages.
    PAYTR_OK_URL: str = "http://localhost:3000/orders/{order_id}/confirmation?payment=ok"
    PAYTR_FAIL_URL: str = "http://localhost:3000/orders/{order_id}/confirmation?payment=failed"
    # How long the payment page stays usable, and how long after that an
    # unpaid card order keeps its stock before it is released.
    PAYTR_TIMEOUT_MINUTES: int = 30
    PAYTR_ABANDON_AFTER_MINUTES: int = 90
    # 0 / 0: installments allowed, up to whatever the card and PayTR offer.
    PAYTR_NO_INSTALLMENT: int = 0
    PAYTR_MAX_INSTALLMENT: int = 0

    # ── Yurtiçi Kargo web service (shipments, labels, tracking) ─────────────
    # Yurtiçi issues two web-service accounts under one customer code, and the
    # account a parcel is registered on is who pays for it: GO ("gönderici
    # ödemeli") for the parcels the shop pays, AO ("alıcı ödemeli") for the
    # karşı ödemeli ones (Order.shipping_payer). In .env only. Until the GO pair
    # is set, "Kargola" takes a tracking code by hand and nothing is sent to
    # Yurtiçi; without the AO pair, karşı ödemeli orders are shipped by hand.
    # Their public test pair is YKTEST / YK.
    YURTICI_MUSTERI_KODU: str = ""
    YURTICI_API_USER_GO: str = ""
    YURTICI_API_PASS_GO: str = ""
    YURTICI_API_USER_AO: str = ""
    YURTICI_API_PASS_AO: str = ""
    # The service's WSDL; requests go to the same address without "?wsdl".
    YURTICI_WSDL_URL: str = "https://ws.yurticikargo.com/KOPSWebServices/ShippingOrderDispatcherServices?wsdl"
    YURTICI_API_LANGUAGE: str = "TR"
    # True: Yurtiçi's test service, whatever the WSDL URL says, where nothing
    # is collected or charged. Set 0 to ship for real.
    YURTICI_TEST_MODE: bool = True
    # The names before the GO/AO split, still read when the new ones are empty:
    # the one account they named is the GO one.
    YURTICI_API_USER: str = ""
    YURTICI_API_PASSWORD: str = ""
    YURTICI_CUSTOMER_CODE: str = ""

    def yurtici_credentials(self, receiver_pays: bool) -> tuple[str, str] | None:
        """The (user, password) a parcel is registered on: AO when the buyer
        pays the carriage, else GO. None while that account is not set."""
        if receiver_pays:
            user, password = self.YURTICI_API_USER_AO, self.YURTICI_API_PASS_AO
        else:
            user = self.YURTICI_API_USER_GO or self.YURTICI_API_USER
            password = self.YURTICI_API_PASS_GO or self.YURTICI_API_PASSWORD
        return (user, password) if user and password else None

    @property
    def yurtici_customer_code(self) -> str:
        return self.YURTICI_MUSTERI_KODU or self.YURTICI_CUSTOMER_CODE

    @property
    def yurtici_configured(self) -> bool:
        return self.yurtici_credentials(receiver_pays=False) is not None

    @property
    def yurtici_receiver_configured(self) -> bool:
        return self.yurtici_credentials(receiver_pays=True) is not None

    @property
    def paytr_configured(self) -> bool:
        return bool(self.PAYTR_MERCHANT_ID and self.PAYTR_MERCHANT_KEY and self.PAYTR_MERCHANT_SALT)


settings = Settings()
