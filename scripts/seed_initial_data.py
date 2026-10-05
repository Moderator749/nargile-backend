"""Seed the 4 core customer segments and a default admin user."""

import asyncio
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.database import async_session
from app.core.security import hash_password
from app.models.user import CustomerSegment, SegmentType, User, UserRole

SEGMENTS = [
    (SegmentType.RETAIL, "Perakende", "Bireysel perakende müşteriler"),
    (SegmentType.CAFE, "Kafe", "Kafe ve restoran işletmeleri"),
    (SegmentType.WHOLESALE, "Toptan", "Toptan alım yapan müşteriler"),
    (SegmentType.DEALER, "Bayi", "Yetkili bayiler"),
]

ADMIN_EMAIL = "admin@nargile.com"
ADMIN_PASSWORD = "Admin123!"


async def seed():
    async with async_session() as session:
        # Seed segments
        for code, label, description in SEGMENTS:
            exists = await session.execute(
                select(CustomerSegment).where(CustomerSegment.code == code)
            )
            if not exists.scalar_one_or_none():
                session.add(
                    CustomerSegment(code=code, label=label, description=description)
                )
                print(f"  + Segment: {label} ({code.value})")
            else:
                print(f"  ~ Segment already exists: {label}")

        await session.commit()

        # Seed admin user
        exists = await session.execute(
            select(User).where(User.email == ADMIN_EMAIL)
        )
        if not exists.scalar_one_or_none():
            admin = User(
                email=ADMIN_EMAIL,
                hashed_password=hash_password(ADMIN_PASSWORD),
                role=UserRole.SUPER_ADMIN,
                is_active=True,
                is_verified=True,
            )
            session.add(admin)
            await session.commit()
            print(f"  + Admin user created: {ADMIN_EMAIL}")
        else:
            print(f"  ~ Admin user already exists: {ADMIN_EMAIL}")

    print("\nSeed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
