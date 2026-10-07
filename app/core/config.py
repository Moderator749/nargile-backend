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


settings = Settings()
