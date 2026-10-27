"""Customers from the old OpenCart store: reading its dump, and their passwords.

The dump comes as phpMyAdmin writes it — extended inserts, many rows per
statement over many lines — or one row per statement; both give every row.
An imported customer signs in with the OpenCart password, and that first
login swaps the old hash for bcrypt.
"""

import hashlib
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import OPENCART_PREFIX, needs_rehash, opencart_hash, verify_password
from app.models.user import User, UserRole

_spec = importlib.util.spec_from_file_location(
    "migrate_customers", Path(__file__).resolve().parents[1] / "scripts" / "migrate_customers_from_opencart.py"
)
migration = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = migration  # its dataclasses look their module up
_spec.loader.exec_module(migration)

LOGIN = "/api/v1/auth/login"
TABLES = {"oc_customer", "oc_address"}


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def _opencart(password: str, salt: str) -> str:
    """What OpenCart 2.x stores: sha1(salt . sha1(salt . sha1(password)))."""
    return _sha1(salt + _sha1(salt + _sha1(password)))


EXTENDED = """\
-- phpMyAdmin SQL Dump
CREATE TABLE `oc_customer` (`customer_id` int(11) NOT NULL);

INSERT INTO `oc_customer` (`customer_id`, `firstname`, `email`, `password`, `salt`, `cart`) VALUES
(1, 'Ali', 'ali@example.com', 'aaaa', 's1', NULL),
(2, 'O\\'Neil', 'b@example.com', 'bbbb', 's2', 'a:1:{i:0;s:4:\\"x),(\\";}'),
(3, 'Çok
satırlı', 'c@example.com', 'cccc', 's3', 'İt''s; fine');
INSERT INTO `oc_product` (`product_id`, `image`) VALUES
(9, _binary 0x00),
(10, 'x');
INSERT INTO `oc_customer` (`customer_id`, `firstname`, `email`, `password`, `salt`, `cart`) VALUES
(4, 'Dördüncü', 'd@example.com', 'dddd', 's4', '');
INSERT INTO `oc_address` (`address_id`, `customer_id`, `city`) VALUES (7, 4, 'Kadıköy'), (8, 4, '-1.5e3');
"""


def test_extended_inserts_give_every_row(tmp_path):
    dump = tmp_path / "backup.sql"
    dump.write_text(EXTENDED)
    rows = migration.parse_dump(dump, TABLES)

    assert [c["customer_id"] for c in rows["oc_customer"]] == ["1", "2", "3", "4"]
    assert rows["oc_customer"][0]["cart"] is None
    # A string keeps its escaped quotes, its "),(" and ";", and its newline.
    assert rows["oc_customer"][1]["firstname"] == "O'Neil"
    assert rows["oc_customer"][1]["cart"] == 'a:1:{i:0;s:4:"x),(";}'
    assert rows["oc_customer"][2]["firstname"] == "Çok\nsatırlı"
    assert rows["oc_customer"][2]["cart"] == "İt's; fine"
    assert rows["oc_customer"][3]["cart"] == ""
    # A one-line statement with several tuples; the unreadable other table is skipped.
    assert [(a["address_id"], a["city"]) for a in rows["oc_address"]] == [("7", "Kadıköy"), ("8", "-1.5e3")]


def test_one_row_per_statement_still_reads(tmp_path):
    dump = tmp_path / "backup.sql"
    dump.write_text(
        "INSERT INTO `oc_customer` (`customer_id`, `email`) VALUES ('1', 'a@example.com');\n"
        "INSERT INTO `oc_customer` (`customer_id`, `email`) VALUES ('2', 'b@example.com');\n"
    )
    assert [c["email"] for c in migration.parse_dump(dump, TABLES)["oc_customer"]] == ["a@example.com", "b@example.com"]


def test_an_unreadable_customer_statement_stops_the_run(tmp_path):
    dump = tmp_path / "backup.sql"
    dump.write_text("INSERT INTO `oc_customer` (`customer_id`, `email`) VALUES\n(1, 'a@example.com'),\n(2, 'unterminated);\n")
    with pytest.raises(migration.DumpError, match="oc_customer"):
        migration.parse_dump(dump, TABLES)


def test_an_opencart_password_verifies_and_asks_for_bcrypt():
    stored = opencart_hash(_opencart("eski-sifre", "Ab3dE6gH9"), "Ab3dE6gH9")
    assert stored.startswith(OPENCART_PREFIX)
    assert verify_password("eski-sifre", stored)
    assert not verify_password("yanlis-sifre", stored)
    assert not verify_password("eski-sifre", opencart_hash(_opencart("eski-sifre", "other"), "Ab3dE6gH9"))
    assert needs_rehash(stored)


def test_the_spellings_opencart_hashed():
    # OpenCart hashed the typed password trimmed and through htmlspecialchars.
    salt = "s4lt"
    assert verify_password("a&b\"c", opencart_hash(_opencart("a&amp;b&quot;c", salt), salt))
    assert verify_password("it's", opencart_hash(_opencart("it&#039;s", salt), salt))
    assert verify_password(" boşluk ", opencart_hash(_opencart("boşluk", salt), salt))
    # OpenCart 1.x: plain md5, no salt.
    assert verify_password("eski", opencart_hash(hashlib.md5(b"eski").hexdigest(), ""))


@pytest.mark.parametrize("value", ["", None, "not-a-hash", "$2y$10$abcdefghijklmnopqrstuv"])
def test_a_dump_value_that_is_no_opencart_hash_is_not_carried(value):
    assert opencart_hash(value, "salt") is None


async def test_the_first_login_swaps_the_old_hash_for_bcrypt(client: AsyncClient, db_session: AsyncSession):
    salt = "Xy7Zq2Lm0"
    user = User(
        email=f"oc-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password=opencart_hash(_opencart("Nargile123", salt), salt),
        role=UserRole.CUSTOMER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()

    wrong = await client.post(LOGIN, json={"email": user.email, "password": "Nargile124"})
    assert wrong.status_code == 401
    await db_session.refresh(user)
    assert user.hashed_password.startswith(OPENCART_PREFIX)

    first = await client.post(LOGIN, json={"email": user.email, "password": "Nargile123"})
    assert first.status_code == 200, first.text
    await db_session.refresh(user)
    assert user.hashed_password.startswith("$2b$")
    assert not needs_rehash(user.hashed_password)

    again = await client.post(LOGIN, json={"email": user.email, "password": "Nargile123"})
    assert again.status_code == 200, again.text


def test_the_migration_carries_the_password(tmp_path):
    salt = "Qw3Er5Ty7"
    digest = _opencart("musteri-sifresi", salt)
    dump = tmp_path / "backup.sql"
    dump.write_text(
        "INSERT INTO `oc_customer` (`customer_id`, `customer_group_id`, `firstname`, `lastname`, `email`, `telephone`,"
        " `password`, `salt`, `newsletter`, `address_id`, `date_added`) VALUES\n"
        f"(1, 1, 'ALİ', 'VELİ', 'ali@example.com', '0532 111 22 33', '{digest}', '{salt}', 0, 0, '2020-01-01 10:00:00'),\n"
        "(2, 1, 'Ayşe', 'Kaya', 'ayse@example.com', '', '', '', 1, 0, '2021-01-01 10:00:00');\n"
    )
    rows = migration.parse_dump(dump, {"oc_customer", "oc_address", "oc_zone", "oc_country"})
    report = migration.Report()
    ali, ayse = migration.prepare(rows, report, None)

    assert verify_password("musteri-sifresi", ali.legacy_password)
    assert ayse.legacy_password is None
    assert [f["customer_id"] for f in report.flagged["password_not_carried"]] == ["2"]
