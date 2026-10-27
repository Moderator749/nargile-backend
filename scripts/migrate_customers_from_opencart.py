"""OpenCart → Sultan Nargile customer migration, from a SQL backup.

Reads the old store's phpMyAdmin/cPanel SQL dump (``INSERT INTO `oc_customer`
…`` with one row per statement, or extended inserts of many rows) and creates an account for every customer, with
their default address, in this application's database. Nothing is fetched
from the network: the dump is the only input.

────────────────────────────────────────────────────────────────────────────
RUNNING IT
────────────────────────────────────────────────────────────────────────────
From the backend folder, with the virtualenv's Python and the database in
.env reachable:

    cd saas_backend
    .venv/bin/python scripts/migrate_customers_from_opencart.py --sql ../backup.sql --dry-run
    .venv/bin/python scripts/migrate_customers_from_opencart.py --sql ../backup.sql --limit 50
    .venv/bin/python scripts/migrate_customers_from_opencart.py --sql ../backup.sql

Options:
    --sql PATH       the SQL backup (default: the newest *_backup.sql one folder
                     up from the backend)
    --dry-run        parse, clean and report, write nothing to the database
    --limit N        only the first N customers of the dump (by customer_id)
    --report PATH    where the JSON report goes (default:
                     scripts/migration_reports/customer-migration-<date>.json)

It is safe to run again: a customer whose e-mail already belongs to an
account is skipped ("already_exists"), never overwritten — the second run
therefore only adds the customers the first one did not.

────────────────────────────────────────────────────────────────────────────
WHAT IT DOES
────────────────────────────────────────────────────────────────────────────
Tables read: oc_customer, oc_address, oc_zone, oc_country, and — when the
dump has them — oc_customer_group / oc_customer_group_description. (The
shop's dump has no group tables: every customer is in group 1, the OpenCart
"Default" group, so they all become RETAIL / Bireysel accounts. Other group
ids are reported, not mapped.)

Each customer becomes:
  users              e-mail (lower-cased, validated), full name (Turkish title
                     case), mobile → phone as +905XXXXXXXXX, a Turkish landline
                     → landline_phone, role CUSTOMER, segment RETAIL, account
                     type INDIVIDUAL, created_at = OpenCart date_added
                     (Europe/Istanbul → UTC), newsletter=1 →
                     marketing_email_consent_at = date_added.
                     The password is the OpenCart one: its hash and salt are
                     stored as "opencart$<salt>$<sha1>" (app.core.security),
                     so the customer signs in with the old password and that
                     first login replaces it with bcrypt. A customer whose
                     dump value is no OpenCart hash gets an unusable password
                     (flagged "password_not_carried") and uses "Şifremi
                     unuttum" (e-mail link or SMS code), which is why the
                     phone and e-mail are cleaned so carefully.
  customer_profiles  one empty profile row, as self-registration creates.
  customer_addresses the customer's DEFAULT address (oc_customer.address_id)
                     as their BILLING address. The schema holds one own address
                     per account, so a customer's other OpenCart addresses are
                     counted and listed in the report, not imported.
                     Turkish address: city = the province (oc_zone), district =
                     OpenCart's free-text "city" field, street = address_1 +
                     address_2 as one line (the checkout's "Açık Adres" shape).
                     Abroad: country = ISO code, city, address lines, postcode.
                     The OpenCart "company" field on an address is reported,
                     not stored — it is free text ("yok", "te"…) and the
                     profile's company title is shown as the customer's name.

Dropped, and listed in the report with the OpenCart customer_id:
  - an e-mail that is not an e-mail address (the account could never log in);
  - the older of two customers with the same e-mail (the newest one is kept);
  - an e-mail already registered in this database.
Flagged, kept, and listed: a phone that is not a usable number (stored as
NULL), a mobile number shared by several customers (only the newest keeps
it, so an SMS code can tell them apart), an address that could not be stored.

Everything is written in ONE transaction: a failure leaves the database as it
was. Take a pg_dump first all the same.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import secrets
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.core.countries import COUNTRY_NAMES, TURKEY  # noqa: E402
from app.core.database import async_session, engine  # noqa: E402
from app.core.security import hash_password, opencart_hash  # noqa: E402
from app.models.user import (  # noqa: E402
    AccountType,
    AddressKind,
    CustomerAddress,
    CustomerProfile,
    CustomerSegment,
    SegmentType,
    User,
    UserRole,
)
from app.schemas.address import TR_PROVINCES, place_key  # noqa: E402
from app.services.notify.phone import normalize_tr_mobile  # noqa: E402

log = logging.getLogger("customer-migration")

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")
OPENCART_TURKEY_COUNTRY_ID = "215"
#: OpenCart country codes that are not ISO 3166-1 alpha-2 any more.
COUNTRY_ALIASES = {"FX": "FR", "UK": "GB"}
#: OpenCart group ids → this application's segment, for dumps without the
#: group tables. 1 is OpenCart's built-in "Default" group.
GROUP_SEGMENTS = {"1": SegmentType.RETAIL}
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
BATCH = 500

# ── SQL dump parsing ──────────────────────────────────────────────────────────

#: The head of one INSERT statement, at the start of a line. Its VALUES list
#: follows: one tuple (the old backup tool's "one row per statement") or many,
#: "(…),\n(…),\n(…);" as phpMyAdmin and mysqldump write extended inserts.
_INSERT = re.compile(
    r"^INSERT(?: IGNORE)? INTO `(?P<table>[A-Za-z0-9_]+)` \((?P<cols>[^)]*)\) VALUES\s*", re.MULTILINE
)
#: One value, with the whitespace around it: a quoted string (which may hold
#: newlines, commas, parentheses and semicolons), NULL, a number, or a hex /
#: bit literal.
_VALUE = re.compile(
    r"\s*(?:'(?P<quoted>(?:[^'\\]+|\\.|'')*)'|(?P<null>NULL)|(?P<number>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
    r"|(?P<literal>0x[0-9A-Fa-f]*|[xXbB]'[0-9A-Fa-f]*'))\s*",
    re.DOTALL,
)
_ESCAPES = {"\\'": "'", '\\"': '"', "\\\\": "\\", "\\n": "\n", "\\r": "\r", "\\t": "\t", "\\0": "\0", "\\Z": "\x1a"}


def _unescape(raw: str) -> str:
    raw = re.sub(r"\\.", lambda m: _ESCAPES.get(m.group(0), m.group(0)[1]), raw, flags=re.DOTALL)
    return raw.replace("''", "'")


class DumpError(ValueError):
    pass


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _tuples(text: str, pos: int) -> tuple[list[list[str | None]], int]:
    """The tuples of one VALUES list starting at `pos`, and where its ';' ends."""
    tuples: list[list[str | None]] = []
    while True:
        pos = _skip_space(text, pos)
        if text[pos : pos + 1] != "(":
            raise DumpError(f"'(' expected at offset {pos}")
        pos += 1
        values: list[str | None] = []
        while True:
            m = _VALUE.match(text, pos)
            if not m:
                raise DumpError(f"unreadable value at offset {pos}")
            if m.group("null"):
                values.append(None)
            elif m.group("number") is not None:
                values.append(m.group("number"))
            elif m.group("literal") is not None:
                values.append(m.group("literal"))
            else:
                values.append(_unescape(m.group("quoted")))
            pos = m.end()
            sep = text[pos : pos + 1]
            pos += 1
            if sep == ")":
                break
            if sep != ",":
                raise DumpError(f"',' or ')' expected at offset {pos - 1}")
        tuples.append(values)
        pos = _skip_space(text, pos)
        sep = text[pos : pos + 1]
        pos += 1
        if sep == ";":
            return tuples, pos
        if sep != ",":
            raise DumpError(f"',' or ';' expected at offset {pos - 1}")


def parse_dump(path: Path, tables: set[str]) -> dict[str, list[dict[str, str | None]]]:
    """Every row of the wanted tables, as {column: value}; NULL → None.

    Each INSERT is read value by value to its closing ';', so a statement may
    carry one row or thousands, over one line or many. A wanted table's
    statement that cannot be read stops the run; another table's is skipped.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    rows: dict[str, list[dict[str, str | None]]] = {t: [] for t in tables}
    pos = 0
    while m := _INSERT.search(text, pos):
        table = m.group("table")
        cols = [c.strip().strip("`") for c in m.group("cols").split(",")]
        try:
            tuples, pos = _tuples(text, m.end())
        except DumpError as exc:
            if table in tables:
                raise DumpError(f"{table}: {exc}") from None
            log.warning("%s: %s; statement skipped", table, exc)
            pos = m.end()
            continue
        if table not in tables:
            continue
        for values in tuples:
            if len(values) != len(cols):
                log.warning("%s: %d columns but %d values, row skipped", table, len(cols), len(values))
                continue
            rows[table].append(dict(zip(cols, values)))
    return rows


# ── Cleaning ──────────────────────────────────────────────────────────────────

_TR_LOWER = str.maketrans("İIŞĞÜÖÇ", "iışğüöç")
_TR_UPPER = str.maketrans("iışğüöç", "İIŞĞÜÖÇ")


def _upper_first(word: str) -> str:
    return word[0].translate(_TR_UPPER).upper() + word[1:] if word else word


def title_case_tr(text: str | None) -> str | None:
    """'MEHMET ALİ' / 'mehmet ali' → 'Mehmet Ali', Turkish letters included;
    also after a hyphen or apostrophe-less compound ('Ali-Can' → 'Ali-Can')."""
    words = (text or "").translate(_TR_LOWER).lower().split()
    out = [
        "-".join(_upper_first(part) for part in w.split("-"))
        for w in words
    ]
    return " ".join(out) or None


_EMAIL_ASCII = str.maketrans("İIıĞğŞşÖöÜüÇç", "iiiggssoouucc")


def clean_email(raw: str | None) -> tuple[str | None, bool]:
    """(the address lower-cased and validated, whether it had to be repaired).

    The old form accepted whatever was typed. The repairs are the ones a
    Turkish keyboard explains: "GMAİL.COM" and "mfazlioğlu82@" (Turkish letters
    in an address that can only be ASCII), "hotmail. com" (a space), and
    "gmailçcom" / "gamil,com" (ç and , sit next to . on the keyboard). Anything
    still not an address afterwards is dropped.
    """
    email = "".join((raw or "").split()).translate(_EMAIL_ASCII).lower()
    email = email.replace("i\u0307", "i")  # İ.lower() leaves a combining dot
    if "@" in email:
        local, _, domain = email.rpartition("@")
        email = f"{local}@{re.sub(r'[.]{2,}', '.', domain.replace(',', '.').replace('ç', '.'))}"
    if not EMAIL.match(email) or len(email) > 255:
        return None, False
    # Lower-casing is not a repair; a changed letter or a dropped space is.
    return email, email != "".join((raw or "").split()).lower()


@dataclass
class Phone:
    mobile: str | None = None  # +905XXXXXXXXX
    landline: str | None = None  # +90XXXXXXXXXX
    foreign: str | None = None  # +<digits>, a number with its own dialling code
    problem: str | None = None  # why nothing usable came out


def clean_phone(raw: str | None, domestic: bool) -> Phone:
    """Turkish numbers as the gateways and the reset flow want them, E.164."""
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return Phone(problem="missing")
    mobile = normalize_tr_mobile(text)
    if mobile:
        return Phone(mobile=f"+{mobile}")
    # A Turkish landline: 10 digits, area code 2xx–4xx, with or without 0 / 90.
    national = digits
    if national.startswith("90") and len(national) == 12:
        national = national[2:]
    elif national.startswith("0") and len(national) == 11:
        national = national[1:]
    if len(national) == 10 and national[0] in "234" and (domestic or digits.startswith("90")):
        return Phone(landline=f"+90{national}")
    if not domestic and 7 <= len(digits) <= 15:
        return Phone(foreign=f"+{digits.lstrip('0') or digits}")
    return Phone(problem="invalid")


_PROVINCE_BY_KEY = {place_key(p): p for p in TR_PROVINCES}


def province(name: str | None) -> str | None:
    return _PROVINCE_BY_KEY.get(place_key(name or ""))


def _trim(value: str | None, limit: int) -> str | None:
    text = " ".join((value or "").split())
    return text[:limit] or None


def _district(raw_city: str | None, prov: str) -> str:
    """OpenCart's free-text city as the ilçe: 'Kozan/adana' → 'Kozan',
    'KADIKÖY/GÖZTEPE' → 'Kadıköy/Göztepe', empty → 'Merkez'."""
    parts = [p for p in re.split(r"\s*[/,\\-]\s*", raw_city or "") if p.strip()]
    kept = [title_case_tr(p) or "" for p in parts if province(p) != prov]
    text = "/".join(p for p in kept if p) or "Merkez"
    return text[:80]


@dataclass
class CleanAddress:
    country: str
    city: str
    district: str | None = None
    street: str | None = None
    postal_code: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None


def clean_address(
    a: dict[str, str | None], zones: dict[str, dict], countries: dict[str, dict]
) -> tuple[CleanAddress | None, str | None]:
    """A CustomerAddress-shaped record, or (None, why not)."""
    zone = zones.get(a.get("zone_id") or "")
    country_row = countries.get(a.get("country_id") or "")
    postcode = _trim(a.get("postcode"), 20)
    if a.get("country_id") == OPENCART_TURKEY_COUNTRY_ID:
        prov = province(zone["name"] if zone else None) or province(a.get("city"))
        if prov is None:
            # No province chosen: take one named in the free text, if any
            # ("Kozan/adana", "İstanbul küçükçekmece halkalı").
            words = re.split(r"[\s/,.\-]+", " ".join(a.get(k) or "" for k in ("city", "address_1", "address_2")))
            prov = next((p for w in words if (p := province(w))), None)
        if prov is None:
            return None, "province unknown"
        street = _trim(", ".join(p for p in ((a.get("address_1") or "").strip(), (a.get("address_2") or "").strip()) if p), 200)
        if street is None:
            return None, "no street"
        return CleanAddress(
            country=TURKEY, city=prov, district=_district(a.get("city"), prov), street=street, postal_code=postcode
        ), None
    code = (country_row or {}).get("iso_code_2") or ""
    code = COUNTRY_ALIASES.get(code, code)
    if code not in COUNTRY_NAMES:
        return None, f"country unknown ({(country_row or {}).get('name') or a.get('country_id')})"
    city = _trim(a.get("city"), 80) or _trim(zone["name"] if zone else None, 80)
    line1 = _trim(a.get("address_1"), 200)
    if city is None or line1 is None:
        return None, "no city or address line"
    return CleanAddress(
        country=code, city=city, address_line1=line1, address_line2=_trim(a.get("address_2"), 200), postal_code=postcode
    ), None


def _added_at(raw: str | None) -> datetime:
    try:
        local = datetime.strptime(raw or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=ISTANBUL_TZ)
    except ValueError:
        return datetime.now(timezone.utc)
    return local.astimezone(timezone.utc)


# ── The migration ─────────────────────────────────────────────────────────────


@dataclass
class Prepared:
    customer_id: str
    email: str
    full_name: str | None
    phone: Phone
    group_id: str
    newsletter: bool
    added_at: datetime
    address: CleanAddress | None
    address_problem: str | None
    address_id: str
    extra_address_ids: list[str] = field(default_factory=list)
    company: str | None = None
    #: The OpenCart hash in its stored form (opencart_hash), or None.
    legacy_password: str | None = None


@dataclass
class Report:
    found: dict = field(default_factory=dict)
    imported_customers: int = 0
    imported_addresses: int = 0
    skipped: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    flagged: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    groups: dict = field(default_factory=dict)
    countries: Counter = field(default_factory=Counter)


def prepare(rows: dict[str, list[dict]], report: Report, limit: int | None) -> list[Prepared]:
    zones = {z["zone_id"]: z for z in rows["oc_zone"]}
    countries = {c["country_id"]: c for c in rows["oc_country"]}
    addresses = {a["address_id"]: a for a in rows["oc_address"]}
    by_customer: dict[str, list[str]] = defaultdict(list)
    for a in rows["oc_address"]:
        by_customer[a["customer_id"] or ""].append(a["address_id"])
    group_names = {
        g["customer_group_id"]: g.get("name") for g in rows.get("oc_customer_group_description", []) if g.get("language_id") in ("1", None)
    }

    customers = sorted(rows["oc_customer"], key=lambda c: int(c["customer_id"] or 0))
    if limit:
        customers = customers[:limit]
    report.found = {
        "customers": len(rows["oc_customer"]),
        "addresses": len(rows["oc_address"]),
        "zones": len(zones),
        "countries": len(countries),
        "customer_groups": len(rows.get("oc_customer_group", [])),
        "considered": len(customers),
    }

    # Same e-mail twice: the newest registration is the one kept.
    newest: dict[str, dict] = {}
    for c in customers:
        email, repaired = clean_email(c["email"])
        if email is None:
            report.skipped["invalid_email"].append({"customer_id": c["customer_id"], "email": c["email"]})
            continue
        if repaired:
            report.flagged["email_repaired"].append({"customer_id": c["customer_id"], "original": c["email"], "email": email})
        other = newest.get(email)
        if other is None or (c["date_added"] or "") > (other["date_added"] or ""):
            if other is not None:
                report.skipped["duplicate_email"].append({"customer_id": other["customer_id"], "email": email, "kept_customer_id": c["customer_id"]})
            newest[email] = c
        else:
            report.skipped["duplicate_email"].append({"customer_id": c["customer_id"], "email": email, "kept_customer_id": other["customer_id"]})

    prepared: list[Prepared] = []
    group_counts: Counter = Counter()
    for email, c in newest.items():
        default_id = c["address_id"] or ""
        own = by_customer.get(c["customer_id"] or "", [])
        raw_address = addresses.get(default_id) or (addresses.get(own[0]) if own else None)
        domestic = raw_address is None or raw_address.get("country_id") == OPENCART_TURKEY_COUNTRY_ID
        address, problem = clean_address(raw_address, zones, countries) if raw_address else (None, "no address")
        if raw_address is None:
            problem = "no address"
        phone = clean_phone(c["telephone"], domestic)
        first = title_case_tr(c["firstname"]) or ""
        last = title_case_tr(c["lastname"]) or ""
        p = Prepared(
            customer_id=c["customer_id"] or "",
            email=email,
            full_name=_trim(f"{first} {last}", 150),
            phone=phone,
            group_id=c["customer_group_id"] or "",
            newsletter=c["newsletter"] == "1",
            added_at=_added_at(c["date_added"]),
            address=address,
            address_problem=problem,
            address_id=raw_address["address_id"] if raw_address else "",
            extra_address_ids=[i for i in own if raw_address is None or i != raw_address["address_id"]],
            company=_trim(raw_address.get("company") if raw_address else None, 255),
            legacy_password=opencart_hash(c.get("password"), c.get("salt")),
        )
        prepared.append(p)
        if p.legacy_password is None:
            report.flagged["password_not_carried"].append({"customer_id": p.customer_id, "email": email})
        group_counts[p.group_id] += 1
        if phone.problem:
            report.flagged[f"phone_{phone.problem}"].append({"customer_id": p.customer_id, "email": email, "telephone": c["telephone"]})
        if phone.landline:
            report.flagged["phone_landline"].append({"customer_id": p.customer_id, "email": email, "landline_phone": phone.landline})
        if problem:
            report.flagged["address_not_imported"].append({"customer_id": p.customer_id, "email": email, "reason": problem})
        if p.extra_address_ids:
            report.flagged["extra_addresses_not_imported"].append({"customer_id": p.customer_id, "email": email, "address_ids": p.extra_address_ids})
        if p.company:
            report.flagged["address_company_not_stored"].append({"customer_id": p.customer_id, "email": email, "company": p.company})
        if address:
            report.countries[address.country] += 1

    # A mobile number shared by several accounts: only the newest keeps it, so
    # an SMS reset code can find exactly one account.
    by_mobile: dict[str, list[Prepared]] = defaultdict(list)
    for p in prepared:
        if p.phone.mobile:
            by_mobile[p.phone.mobile].append(p)
    for number, group in by_mobile.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda p: p.added_at, reverse=True)
        for p in group[1:]:
            p.phone.mobile = None
            p.phone.problem = "duplicate"
        report.flagged["phone_duplicate"].append(
            {"phone": number, "kept_customer_id": group[0].customer_id, "cleared_customer_ids": [p.customer_id for p in group[1:]]}
        )

    report.groups = {
        gid: {
            "name": group_names.get(gid) or ("Default" if gid == "1" else None),
            "segment": GROUP_SEGMENTS.get(gid, SegmentType.RETAIL).value,
            "mapped": gid in GROUP_SEGMENTS,
            "customers": n,
        }
        for gid, n in sorted(group_counts.items())
    }
    return prepared


async def write(prepared: list[Prepared], report: Report) -> None:
    async with async_session() as db:
        segment_ids = {code: sid for sid, code in (await db.execute(select(CustomerSegment.id, CustomerSegment.code))).all()}
        if SegmentType.RETAIL not in segment_ids:
            raise RuntimeError("RETAIL segment missing — run seed_initial_data.py first")
        existing = {
            e for (e,) in (await db.execute(select(func.lower(User.email)).where(func.lower(User.email).in_([p.email for p in prepared])))).all()
        }
        # A customer keeps the OpenCart password (its hash, replaced by bcrypt
        # at the first login). One whose dump value is no OpenCart hash gets an
        # unusable password instead: a random secret hashed once (bcrypt is too
        # slow to do thousands of times) and never kept; they use "Şifremi unuttum".
        unusable = hash_password(secrets.token_urlsafe(32))

        pending = 0
        for p in prepared:
            if p.email in existing:
                report.skipped["already_exists"].append({"customer_id": p.customer_id, "email": p.email})
                continue
            user = User(
                email=p.email,
                full_name=p.full_name,
                hashed_password=p.legacy_password or unusable,
                phone=p.phone.mobile or p.phone.foreign,
                landline_phone=p.phone.landline,
                account_type=AccountType.INDIVIDUAL,
                segment_id=segment_ids[GROUP_SEGMENTS.get(p.group_id, SegmentType.RETAIL)],
                marketing_email_consent_at=p.added_at if p.newsletter else None,
                is_active=True,
                is_verified=False,
                role=UserRole.CUSTOMER,
                created_at=p.added_at,
                updated_at=p.added_at,
            )
            db.add(user)
            await db.flush()
            db.add(CustomerProfile(user_id=user.id))
            if p.address:
                db.add(CustomerAddress(user_id=user.id, kind=AddressKind.BILLING, **p.address.__dict__))
                report.imported_addresses += 1
            report.imported_customers += 1
            pending += 1
            if pending >= BATCH:
                await db.flush()
                log.info("… %d customers written", report.imported_customers)
                pending = 0
        await db.commit()


async def verify() -> dict:
    async with async_session() as db:
        customers = await db.scalar(select(func.count()).select_from(User).where(User.role == UserRole.CUSTOMER))
        addresses = await db.scalar(select(func.count()).select_from(CustomerAddress))
        profiles = await db.scalar(select(func.count()).select_from(CustomerProfile))
    return {"users_with_role_customer": customers, "customer_addresses": addresses, "customer_profiles": profiles}


async def write_and_verify(prepared: list[Prepared], report: Report) -> dict:
    """One event loop for both: the engine's pooled connections belong to the
    loop that opened them, and a second asyncio.run() cannot use them."""
    try:
        await write(prepared, report)
        return await verify()
    finally:
        await engine.dispose()


def _default_sql() -> Path | None:
    dumps = sorted((Path(__file__).resolve().parents[2]).glob("*_backup.sql"))
    return dumps[-1] if dumps else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sql", type=Path, default=_default_sql())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--report", type=Path)
    opts = parser.parse_args()
    if not opts.sql or not opts.sql.exists():
        parser.error("--sql: backup file not found")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    engine.echo = False

    started = datetime.now(timezone.utc)
    rows = parse_dump(
        opts.sql,
        {"oc_customer", "oc_address", "oc_zone", "oc_country", "oc_customer_group", "oc_customer_group_description"},
    )
    report = Report()
    prepared = prepare(rows, report, opts.limit)
    log.info("parsed %s: %d customers, %d addresses; %d prepared", opts.sql.name, report.found["customers"], report.found["addresses"], len(prepared))

    after = None
    if opts.dry_run:
        report.imported_customers = len(prepared)
        report.imported_addresses = sum(1 for p in prepared if p.address)
    else:
        after = asyncio.run(write_and_verify(prepared, report))

    out = {
        "source": str(opts.sql),
        "run_at": started.isoformat(),
        "dry_run": opts.dry_run,
        "found_in_sql": report.found,
        "imported_customers": report.imported_customers,
        "imported_addresses": report.imported_addresses,
        "skipped_counts": {k: len(v) for k, v in report.skipped.items()},
        "flagged_counts": {k: len(v) for k, v in report.flagged.items()},
        "customer_groups": report.groups,
        "address_countries": dict(report.countries.most_common()),
        "database_after": after,
        "skipped": dict(report.skipped),
        "flagged": dict(report.flagged),
        "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
    }
    path = opts.report or Path(__file__).parent / "migration_reports" / f"customer-migration-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    print()
    print(f"  source              {opts.sql.name}{'  (DRY RUN: nothing written)' if opts.dry_run else ''}")
    print(f"  found in SQL        {report.found['customers']} customers, {report.found['addresses']} addresses")
    print(f"  imported customers  {report.imported_customers}")
    print(f"  imported addresses  {report.imported_addresses}")
    for k, v in out["skipped_counts"].items():
        print(f"  skipped {k:<19} {v}")
    for k, v in out["flagged_counts"].items():
        print(f"  flagged {k:<19} {v}")
    groups = ", ".join(f"{g} ({d['name']}→{d['segment']}): {d['customers']}" for g, d in report.groups.items())
    print(f"  groups              {groups}")
    if after:
        print(f"  database after      {after}")
    print(f"  report              {path}")


if __name__ == "__main__":
    main()
