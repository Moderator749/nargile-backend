from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.user import LoginRequest, Token, UserCreate, UserMe, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    # Self-service sign-ups are always RETAIL. B2B segments (CAFE/WHOLESALE/DEALER)
    # unlock lower prices and are assigned by admins only.
    retail_id = await db.scalar(
        select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL)
    )
    if retail_id is None:
        raise HTTPException(status_code=500, detail="Default segment not configured")

    full_name = payload.full_name.strip() if payload.full_name else None
    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=full_name or None,
        phone=payload.phone,
        segment_id=retail_id,
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


@router.get("/me", response_model=UserMe)
async def read_me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Fetch the code explicitly: lazy-loading user.segment fails under AsyncSession.
    segment_code = None
    if user.segment_id is not None:
        segment_code = await db.scalar(
            select(CustomerSegment.code).where(CustomerSegment.id == user.segment_id)
        )
    return UserMe.model_validate(user).model_copy(update={"segment_code": segment_code})
