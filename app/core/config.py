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
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
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

    @property
    def paytr_configured(self) -> bool:
        return bool(self.PAYTR_MERCHANT_ID and self.PAYTR_MERCHANT_KEY and self.PAYTR_MERCHANT_SALT)


settings = Settings()
