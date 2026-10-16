"""E-mail and SMS behind swappable drivers, every message logged.

    from app.services.notify import dispatch
    await dispatch.send_email(db, to=..., subject=..., text=..., purpose="password_reset")
    await dispatch.send_sms(db, to=..., text=..., purpose="password_reset")

Which driver handles a message is settings (EMAIL_DRIVER / SMS_DRIVER, see
app.core.config). Without credentials the mock driver is used: it logs the
message, records it, and sends nothing — development and the tests never reach
a real provider.
"""
