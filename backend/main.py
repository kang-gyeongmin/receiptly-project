from fastapi              import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib           import asynccontextmanager
from models.mongo         import mongodb
from routers              import receipt, chat

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 시작
    mongodb.connect()
    yield
    # 종료
    mongodb.disconnect()

app = FastAPI(
    title       = "Receiptly API",
    description = "AI 가계부 백엔드",
    version     = "0.1.0 (Phase 1)",
    lifespan    = lifespan
)

# CORS (React Native 앱에서 접근 허용)
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],   # Phase 4 배포 시 도메인 제한
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# 라우터 등록
app.include_router(receipt.router)
app.include_router(chat.router)

@app.get("/")
async def root():
    return {
        "service": "Receiptly",
        "phase":   1,
        "status":  "running"
    }

@app.get("/health")
async def health():
    return {"status": "ok"}