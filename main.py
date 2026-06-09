from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from dotenv import load_dotenv
import os

load_dotenv()

app = FastAPI()
client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME")]

# ── 웹 입력 화면 ───────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <html>
    <head>
        <title>Receiptly</title>
        <style>
            body { font-family: sans-serif; max-width: 500px; margin: 60px auto; }
            input, textarea { width: 100%; padding: 8px; margin: 6px 0 16px; box-sizing: border-box; }
            button { background: #4A90D9; color: white; padding: 10px 24px; border: none; cursor: pointer; border-radius: 6px; }
            h2 { color: #1A1A2E; }
        </style>
    </head>
    <body>
        <h2>🧾 Receiptly</h2>

        <h3>텍스트 입력</h3>
        <form action="/add/text" method="post">
            <input name="store_name" placeholder="가게명" required />
            <input name="amount"     placeholder="금액 (숫자)" type="number" required />
            <input name="date"       placeholder="날짜 (YYYY-MM-DD)" required />
            <textarea name="memo"    placeholder="메모 (선택)"></textarea>
            <button type="submit">저장</button>
        </form>

        <hr style="margin: 40px 0" />

        <h3>이미지 업로드</h3>
        <form action="/add/image" method="post" enctype="multipart/form-data">
            <input type="file" name="file" accept="image/*" required />
            <button type="submit">업로드</button>
        </form>
    </body>
    </html>
    """

# ── 텍스트 저장 ────────────────────────────────────────
@app.post("/add/text")
async def add_text(
    store_name: str = Form(...),
    amount:     int = Form(...),
    date:       str = Form(...),
    memo:       str = Form("")
):
    doc = {
        "type":       "text",
        "store_name": store_name,
        "amount":     amount,
        "date":       date,
        "memo":       memo,
        "created_at": datetime.utcnow().isoformat()
    }
    await db.expenses.insert_one(doc)
    return HTMLResponse("<script>alert('저장 완료!'); location.href='/'</script>")

# ── 이미지 저장 ────────────────────────────────────────
@app.post("/add/image")
async def add_image(file: UploadFile = File(...)):
    contents = await file.read()
    doc = {
        "type":      "image",
        "filename":  file.filename,
        "size":      len(contents),
        "data":      contents,          # 3단계에서 OCR로 교체 예정
        "created_at": datetime.utcnow().isoformat()
    }
    await db.images.insert_one(doc)
    return HTMLResponse("<script>alert('업로드 완료!'); location.href='/'</script>")

# ── 저장된 데이터 확인 ─────────────────────────────────
@app.get("/list")
async def list_expenses():
    result = []
    async for doc in db.expenses.find().sort("created_at", -1).limit(20):
        doc["_id"] = str(doc["_id"])
        result.append(doc)
    return result

@app.get("/list/images")
async def list_images():
    result = []
    async for doc in db.images.find().sort("created_at", -1).limit(20):
        doc["_id"] = str(doc["_id"])
        doc.pop("data", None)  # 바이너리 데이터는 제외하고 메타만 반환
        result.append(doc)
    return result