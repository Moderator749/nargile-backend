from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import CustomerSegment, User
from app.schemas.user import LoginRequest, Token, UserCreate, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    segment_id = None
    if payload.segment_code:
        seg = await db.execute(
            select(CustomerSegment).where(CustomerSegment.code == payload.segment_code)
        )
        segment = seg.scalar_one_or_none()
        if not segment:
            raise HTTPException(status_code=400, detail="Invalid segment")
        segment_id = segment.id

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        phone=payload.phone,
        segment_id=segment_id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=Token)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token(data={"sub": str(user.id), "role": user.role})
    return Token(access_token=token)
