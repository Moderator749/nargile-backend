from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import settings

app = FastAPI(
    title="Nargile SaaS Backend",
    version="0.1.0",
    debug=settings.DEBUG,
)

app.include_router(api_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
