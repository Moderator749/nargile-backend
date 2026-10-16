from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Channel = Literal["email", "sms"]
# "all": every customer; a segment code; or "manual" (addresses typed in).
Audience = Literal["all", "RETAIL", "CAFE", "WHOLESALE", "DEALER", "manual"]

SMS_MAX_CHARS = 918  # six UCS-2 parts / six GSM-7 parts is 918
EMAIL_BODY_MAX = 50_000
MANUAL_MAX = 500


class PasswordResetRequest(BaseModel):
    channel: Channel
    # The e-mail address or username (e-mail), or the mobile number (SMS).
    identifier: str = Field(min_length=3, max_length=255)


class PasswordResetConfirm(BaseModel):
    token: str | None = Field(default=None, max_length=200)  # from the e-mailed link
    phone: str | None = Field(default=None, max_length=30)  # with the SMS code
    code: str | None = Field(default=None, pattern=r"^\s*\d{6}\s*$")
    new_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def _one_way(self) -> "PasswordResetConfirm":
        if not self.token and not (self.phone and self.code):
            raise ValueError("Bağlantıdaki anahtar ya da telefon numarası ile kod gerekli")
        return self


class PasswordResetCheck(BaseModel):
    token: str = Field(max_length=200)


class CommunicationPreferences(BaseModel):
    """Ticari elektronik ileti onayı, per channel."""

    email: bool
    sms: bool


class CampaignCreate(BaseModel):
    channel: Channel
    audience: Audience
    manual_recipients: list[str] = Field(default_factory=list, max_length=MANUAL_MAX)
    # For typed-in recipients: the admin vouches they agreed to campaign messages.
    consent_confirmed: bool = False
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=EMAIL_BODY_MAX)
    body_format: Literal["text", "html"] = "text"

    @model_validator(mode="after")
    def _consistent(self) -> "CampaignCreate":
        self.body = self.body.strip()
        if not self.body:
            raise ValueError("Mesaj boş olamaz")
        if self.channel == "email":
            self.subject = (self.subject or "").strip()
            if not self.subject:
                raise ValueError("E-posta için bir konu girin")
        else:
            self.subject, self.body_format = None, "text"
            if len(self.body) > SMS_MAX_CHARS:
                raise ValueError(f"SMS en fazla {SMS_MAX_CHARS} karakter olabilir")
        if self.audience == "manual":
            self.manual_recipients = [r.strip() for r in self.manual_recipients if r.strip()]
            if not self.manual_recipients:
                raise ValueError("En az bir alıcı girin")
            if not self.consent_confirmed:
                raise ValueError("Elle girilen alıcıların ticari ileti onayı olduğunu onaylayın")
        return self


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel: Channel
    audience: str
    subject: str | None
    body: str
    body_format: str
    sms_segments: int | None
    status: str
    total: int
    sent: int
    failed: int
    created_at: datetime
    finished_at: datetime | None


class NotificationLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel: Channel
    recipient: str
    purpose: str
    campaign_id: int | None
    subject: str | None
    provider: str
    status: str
    error: str | None
    created_at: datetime
    sent_at: datetime | None


class AudienceCount(BaseModel):
    key: Audience
    label: str
    total: int  # customers in the group
    eligible: int  # of them: consent given and reachable on the channel


class DriverStatus(BaseModel):
    email: str  # the driver in use: "smtp", "resend" or "mock"
    sms: str
    sms_opt_out_text: str
