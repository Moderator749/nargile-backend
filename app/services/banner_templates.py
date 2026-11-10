"""Ready-made banner presets for the year's special days ("Hazır metinler").

The Turkish national days, the two bayrams and the shop calendar's own days,
each with its range (Europe/Istanbul, the whole day unless said otherwise),
its text and its look — the next one to come, so 2026's last days and 2027's
first. Only 10 Kasım publishes by itself; the rest wait, off, for the admin to
switch them on. The sales days count down to their end; a memorial day never
does.

`apply` writes them idempotently, by title. A preset seeded before titles
existed (a8d2f6b31c74) is recognised by its original text and adopted, and on
a preset already there only what is empty is filled in — the admin's text,
dates and switch stay as they are — except for 10 Kasım, whose solemn look,
day and automatic publishing are made sure of. A template that `replaces`
another (Hayırlı Cumalar for Efsane Cuma) takes that preset over whole: its
title, text, look, range and countdown, the switch kept.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.site import BannerPreset

TR = ZoneInfo("Europe/Istanbul")

BLACK, WHITE = "#000000", "#ffffff"
# Night colours for the commemorations; the bayrams in the palace's green and gold.
MOURNING = "#111827"
SULTAN_GREEN, SULTAN_GOLD = "#14532d", "#fde68a"
SPRING_GREEN, SCHOOL_BLUE, ROSE, PLUM = "#166534", "#1e3a8a", "#9d174d", "#6b21a8"


def _day(date: str, last: str | None = None) -> tuple[datetime, datetime]:
    """From 00:00 on `date` to 23:59:59 on `last` (or the same day), Turkey time."""
    start = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=TR)
    end = datetime.fromisoformat(f"{last or date}T23:59:59").replace(tzinfo=TR)
    return start, end


@dataclass(frozen=True)
class Template:
    title: str
    message: str
    icon: str
    days: tuple[datetime, datetime]
    background: str | None
    text: str | None = None
    # Published by itself within its range.
    scheduled: bool = False
    # Kept as it is on an existing preset: look, range and switch.
    enforced: bool = False
    # The banner counts down to the range's end (a sale).
    countdown: bool = False
    # The title of a preset this one takes the place of, rewritten whole.
    replaces: str | None = None
    # The text a8d2f6b31c74 seeded it with, before presets had titles.
    seeded_as: str | None = None


TEMPLATES: tuple[Template, ...] = (
    Template(
        "10 Kasım Atatürk'ü Anma Günü",
        "Ulu Önder Gazi Mustafa Kemal Atatürk'ü aramızdan ayrılışının 88. yılında saygı, sevgi ve özlemle anıyoruz.",
        "memorial",
        _day("2026-11-10"),
        BLACK,
        WHITE,
        scheduled=True,
        enforced=True,
        seeded_as="10 Kasım: Ulu Önder Mustafa Kemal Atatürk'ü saygı, sevgi ve özlemle anıyoruz.",
    ),
    Template(
        "11.11 Alışveriş Festivali",
        "11.11 Alışveriş Festivali Sultan Nargile'de: seçili nargile takımlarında bugüne özel fırsatlar!",
        "campaign",
        _day("2026-11-11"),
        "festive_red",
        countdown=True,
    ),
    Template(
        "24 Kasım Öğretmenler Günü",
        "24 Kasım Öğretmenler Günü Kutlu Olsun! Emeği geçen tüm öğretmenlerimize teşekkürler.",
        "celebration",
        _day("2026-11-24"),
        SCHOOL_BLUE,
        WHITE,
    ),
    Template(
        "Hayırlı Cumalar",
        "Hayırlı Cumalar! Kasım'ın son cumasına özel fırsatlar Sultan Nargile'de!",
        "campaign",
        _day("2026-11-27"),
        SULTAN_GREEN,
        SULTAN_GOLD,
        countdown=True,
        replaces="Efsane Cuma (Black Friday)",
    ),
    Template(
        "22 Aralık Sarıkamış Şehitlerini Anma Günü",
        "Sarıkamış Harekâtı'nda donarak şehit düşen kahraman askerlerimizi rahmet ve minnetle anıyoruz.",
        "memorial",
        _day("2026-12-22"),
        MOURNING,
        WHITE,
    ),
    Template(
        "Yılbaşı",
        "Mutlu Yıllar! Yeni yılda bereket ve bolluk dileriz.",
        "celebration",
        _day("2026-12-31", "2027-01-01"),
        "festive_red",
        seeded_as="Mutlu Yıllar! Yeni yılda bereket ve bolluk dileriz.",
    ),
    Template(
        "Sevgililer Günü",
        "Sevgililer Günü'nde keyfi paylaşmanın en şık hâli: hediyelik nargile takımları Sultan Nargile'de.",
        "campaign",
        _day("2027-02-10", "2027-02-14"),
        "#9f1239",
        countdown=True,
    ),
    Template(
        "8 Mart Dünya Kadınlar Günü",
        "8 Mart Dünya Kadınlar Günü Kutlu Olsun!",
        "celebration",
        _day("2027-03-08"),
        PLUM,
        WHITE,
    ),
    Template(
        "12 Mart İstiklal Marşı'nın Kabulü",
        "İstiklal Marşı'mızın kabulünün yıl dönümünde milli şairimiz Mehmet Akif Ersoy'u rahmet ve saygıyla anıyoruz.",
        "memorial",
        _day("2027-03-12"),
        "crimson_gradient",
    ),
    Template(
        "Ramazan Bayramı",
        "Ramazan Bayramınız Mübarek Olsun!",
        "celebration",
        _day("2027-03-09", "2027-03-11"),
        SULTAN_GREEN,
        SULTAN_GOLD,
        seeded_as="Ramazan Bayramınız Mübarek Olsun!",
    ),
    Template(
        "18 Mart Çanakkale Zaferi",
        "18 Mart Çanakkale Zaferi'nin 112. yıl dönümünde aziz şehitlerimizi rahmet ve minnetle anıyoruz.",
        "memorial",
        _day("2027-03-18"),
        MOURNING,
        WHITE,
    ),
    Template(
        "21 Mart Nevruz Bayramı",
        "Baharın müjdecisi Nevruz Bayramınız Kutlu Olsun!",
        "celebration",
        _day("2027-03-21"),
        SPRING_GREEN,
        WHITE,
    ),
    Template(
        "23 Nisan Ulusal Egemenlik ve Çocuk Bayramı",
        "23 Nisan Ulusal Egemenlik ve Çocuk Bayramı Kutlu Olsun!",
        "memorial",
        _day("2027-04-23"),
        "crimson_gradient",
        seeded_as="23 Nisan Ulusal Egemenlik ve Çocuk Bayramı Kutlu Olsun!",
    ),
    Template(
        "29 Nisan Kut'ül Amare Zaferi",
        "Kut'ül Amare Zaferi'nin yıl dönümünde kahraman şehitlerimizi rahmet ve minnetle anıyoruz.",
        "memorial",
        _day("2027-04-29"),
        "crimson_gradient",
    ),
    Template(
        "1 Mayıs Emek ve Dayanışma Günü",
        "1 Mayıs Emek ve Dayanışma Günü Kutlu Olsun!",
        "celebration",
        _day("2027-05-01"),
        "dark_luxury",
    ),
    Template(
        "Anneler Günü",
        "Anneler Günü'ne özel: annenize yakışan hediyelik nargile takımları Sultan Nargile'de!",
        "campaign",
        _day("2027-05-03", "2027-05-09"),
        ROSE,
        WHITE,
        countdown=True,
    ),
    Template(
        "Kurban Bayramı",
        "Kurban Bayramınız Mübarek Olsun!",
        "celebration",
        _day("2027-05-16", "2027-05-19"),
        SULTAN_GREEN,
        SULTAN_GOLD,
        seeded_as="Kurban Bayramınız Mübarek Olsun!",
    ),
    Template(
        "19 Mayıs Atatürk'ü Anma, Gençlik ve Spor Bayramı",
        "19 Mayıs Atatürk'ü Anma, Gençlik ve Spor Bayramı Kutlu Olsun!",
        "memorial",
        _day("2027-05-19"),
        "crimson_gradient",
        seeded_as="19 Mayıs Atatürk'ü Anma, Gençlik ve Spor Bayramı Kutlu Olsun!",
    ),
    Template(
        "29 Mayıs İstanbul'un Fethi",
        "İstanbul'un Fethi'nin yıl dönümü kutlu olsun! Fatih Sultan Mehmet Han'ı ve şehitlerimizi rahmetle anıyoruz.",
        "memorial",
        _day("2027-05-29"),
        "crimson_gradient",
    ),
    Template(
        "Babalar Günü",
        "Babalar Günü'ne özel: babanıza yakışan nargile takımları ve aksesuarlar Sultan Nargile'de.",
        "campaign",
        _day("2027-06-14", "2027-06-20"),
        "#1e293b",
        countdown=True,
    ),
    Template(
        "15 Temmuz Demokrasi ve Milli Birlik Günü",
        "15 Temmuz Demokrasi ve Milli Birlik Günü'nde şehitlerimizi rahmet ve minnetle anıyoruz.",
        "memorial",
        _day("2027-07-15"),
        MOURNING,
        WHITE,
    ),
    Template(
        "26 Ağustos Malazgirt Zaferi",
        "Anadolu'nun kapılarını açan Malazgirt Zaferi'nin yıl dönümü kutlu olsun!",
        "memorial",
        _day("2027-08-26"),
        "crimson_gradient",
    ),
    Template(
        "30 Ağustos Zafer Bayramı",
        "30 Ağustos Zafer Bayramımız Kutlu Olsun!",
        "memorial",
        _day("2027-08-30"),
        "crimson_gradient",
    ),
    Template(
        "29 Ekim Cumhuriyet Bayramı",
        "29 Ekim Cumhuriyet Bayramımız Kutlu Olsun!",
        "memorial",
        _day("2027-10-29"),
        "crimson_gradient",
        seeded_as="29 Ekim Cumhuriyet Bayramımız Kutlu Olsun!",
    ),
)


@dataclass
class Change:
    title: str
    action: str  # "added" | "updated" | "unchanged"
    fields: list[str]


async def apply(db: AsyncSession) -> list[Change]:
    """Write the templates (flushed, not committed); what each one did."""
    rows = list((await db.scalars(select(BannerPreset).order_by(BannerPreset.sort_order, BannerPreset.id))).all())
    next_order = (await db.scalar(select(func.max(BannerPreset.sort_order))) or -1) + 1
    changes: list[Change] = []
    for t in TEMPLATES:
        row = next((r for r in rows if r.title == t.title), None) or next(
            (r for r in rows if r.title is None and t.seeded_as and r.message == t.seeded_as), None
        )
        replaced = row is None and t.replaces is not None
        if replaced:
            row = next((r for r in rows if r.title == t.replaces), None)
        if row is None:
            row = BannerPreset(
                title=t.title,
                message=t.message,
                icon_type=t.icon,
                starts_at=t.days[0],
                ends_at=t.days[1],
                background_style=t.background,
                text_color=t.text,
                is_scheduled=t.scheduled,
                show_countdown=t.countdown,
                sort_order=next_order,
            )
            next_order += 1
            db.add(row)
            rows.append(row)
            changes.append(Change(t.title, "added", []))
            continue

        wanted: dict = {}
        if replaced:
            wanted = {
                "title": t.title,
                "message": t.message,
                "icon_type": t.icon,
                "background_style": t.background,
                "text_color": t.text,
                "starts_at": t.days[0],
                "ends_at": t.days[1],
                "show_countdown": t.countdown,
            }
        if row.title is None:
            wanted["title"] = t.title
        if t.enforced or (row.background_style is None and row.text_color is None):
            wanted |= {"background_style": t.background, "text_color": t.text}
        if t.enforced or (row.starts_at is None and row.ends_at is None):
            wanted |= {"starts_at": t.days[0], "ends_at": t.days[1]}
        if t.enforced:
            wanted["is_scheduled"] = t.scheduled
        changed = [field for field, value in wanted.items() if getattr(row, field) != value]
        for field in changed:
            setattr(row, field, wanted[field])
        changes.append(Change(t.title, "updated" if changed else "unchanged", changed))
    await db.flush()
    return changes
