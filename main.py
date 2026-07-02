from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Cookie
from fastapi.responses import HTMLResponse, JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import re
import calendar
import hashlib
import secrets
from typing import Optional
import easyocr
from PIL import Image, ImageEnhance, ImageOps
from io import BytesIO

load_dotenv()

app = FastAPI()
client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME")]

# OCR 리더 초기화
try:
    reader = easyocr.Reader(['ko', 'en'])
except Exception as e:
    print(f"⚠️ OCR 초기화 실패: {e}")
    reader = None

# 기본 카테고리
DEFAULT_CATEGORIES = ["카페", "식사", "데이트", "고정지출"]

# ── 유틸 함수 ─────────────────────────────────────
def validate_password(password: str) -> tuple[bool, str]:
    """비밀번호 검증 (8자 이상, 영어, 숫자, 특수문자(!,#,$))"""
    if len(password) < 8:
        return False, "비밀번호는 8자 이상이어야 합니다"

    if not re.search(r'[a-zA-Z]', password):
        return False, "비밀번호에 영어(a-z, A-Z)가 포함되어야 합니다"

    if not re.search(r'[0-9]', password):
        return False, "비밀번호에 숫자(0-9)가 포함되어야 합니다"

    if not re.search(r'[!#$]', password):
        return False, "비밀번호에 특수문자(!, #, $)가 포함되어야 합니다"

    return True, "안전한 비밀번호입니다"

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def generate_token() -> str:
    return secrets.token_urlsafe(32)

async def get_current_user(session: Optional[str] = Cookie(None)):
    if not session:
        return None
    session_doc = await db.sessions.find_one({"token": session})
    if not session_doc:
        return None
    return await db.users.find_one({"_id": session_doc["user_id"]})

# ── ⚠️ 미사용 (Phase 2 재연결 예정) ────────────────────
# 아래 3개 함수(preprocess_image / extract_text_from_image / parse_natural_language)는
# 구현돼 있지만 현재 어떤 엔드포인트/UI에도 연결돼 있지 않음.
# 영수증 OCR·자연어 입력 기능을 붙일 때 재사용 예정. (삭제 금지)
def preprocess_image(image: Image.Image) -> Image.Image:
    width, height = image.size
    if width < 800 or height < 600:
        scale = max(2, 1000 // max(width, height))
        image = image.resize((width * scale, height * scale), Image.Resampling.LANCZOS)

    try:
        image = ImageOps.exif_transpose(image)
    except:
        pass

    image_gray = image.convert('L')
    enhancer = ImageEnhance.Contrast(image_gray)
    image_gray = enhancer.enhance(1.5)
    enhancer = ImageEnhance.Brightness(image_gray)
    image_gray = enhancer.enhance(1.1)
    enhancer = ImageEnhance.Sharpness(image_gray)
    image_gray = enhancer.enhance(2.0)

    return image_gray

def extract_text_from_image(image_bytes: bytes) -> str:
    if reader is None:
        return ""

    try:
        image = Image.open(BytesIO(image_bytes))
        image = preprocess_image(image)
        results = reader.readtext(image, detail=1)
        lines = []
        for result in results:
            text = result[1]
            confidence = result[2]
            if confidence >= 0.3 and text.strip():
                lines.append((result[0][0][1], text))
        lines.sort(key=lambda x: x[0])
        extracted_text = " ".join([text for _, text in lines])
        extracted_text = re.sub(r'\s+', ' ', extracted_text).strip()
        return extracted_text
    except Exception as e:
        print(f"OCR 오류: {e}")
        return ""

def parse_natural_language(text: str) -> dict:
    text = text.strip()
    result = {"date": None, "store_name": None, "amount": None, "memo": ""}

    amount_match = re.search(r'(\d+)\s*원?$', text)
    if amount_match:
        result["amount"] = int(amount_match.group(1))
        text = text[:amount_match.start()].strip()

    date_match = re.search(r'(\d{1,2})[/-](\d{1,2})', text)
    if date_match:
        month, day = date_match.group(1), date_match.group(2)
        today = datetime.now()
        year = today.year
        if int(month) > today.month:
            year -= 1
        result["date"] = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        text = text[:date_match.start()] + text[date_match.end():]

    store = text.strip()
    if store:
        result["store_name"] = store

    if not result["date"]:
        result["date"] = datetime.now().strftime("%Y-%m-%d")

    return result
# ── ⚠️ 미사용 블록 끝 ──────────────────────────────────

# ── 인증 ────────────────────────────────────────
@app.post("/auth/check-username")
async def check_username(username: str = Form(...)):
    """아이디 중복 확인"""
    if not username or len(username) < 4:
        return JSONResponse({"available": False, "message": "아이디는 4자 이상이어야 합니다"})

    existing = await db.users.find_one({"username": username})
    if existing:
        return JSONResponse({"available": False, "message": "이미 사용 중인 아이디입니다"})

    return JSONResponse({"available": True, "message": "사용 가능한 아이디입니다"})

@app.post("/auth/signup")
async def signup(username: str = Form(...), password: str = Form(...), password2: str = Form(...)):
    # 아이디 검증
    if not username or len(username) < 4:
        return JSONResponse({"error": "아이디는 4자 이상이어야 합니다"}, status_code=400)

    existing = await db.users.find_one({"username": username})
    if existing:
        return JSONResponse({"error": "이미 사용 중인 아이디입니다"}, status_code=400)

    # 비밀번호 검증
    if password != password2:
        return JSONResponse({"error": "비밀번호가 일치하지 않습니다"}, status_code=400)

    is_valid, message = validate_password(password)
    if not is_valid:
        return JSONResponse({"error": message}, status_code=400)

    # 사용자 생성
    user = {
        "username": username,
        "password": hash_password(password),
        "categories": DEFAULT_CATEGORIES,
        "created_at": datetime.now().isoformat()
    }
    result = await db.users.insert_one(user)

    # 자동 로그인
    token = generate_token()
    await db.sessions.insert_one({"user_id": result.inserted_id, "token": token})

    response = JSONResponse({"status": "success", "message": "회원가입 완료되었습니다"})
    response.set_cookie("session", token, httponly=True, max_age=86400*30)
    return response

@app.post("/auth/login")
async def login(username: str = Form(...), password: str = Form(...)):
    user = await db.users.find_one({"username": username, "password": hash_password(password)})
    if not user:
        return JSONResponse({"error": "계정 정보가 일치하지 않습니다"}, status_code=400)

    token = generate_token()
    await db.sessions.insert_one({"user_id": user["_id"], "token": token})

    response = JSONResponse({"status": "success"})
    response.set_cookie("session", token, httponly=True, max_age=86400*30)
    return response

@app.post("/auth/logout")
async def logout(session: Optional[str] = Cookie(None)):
    if session:
        await db.sessions.delete_one({"token": session})

    response = JSONResponse({"status": "success"})
    response.delete_cookie("session")
    return response

# ── 메인 페이지 ────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)

    if not user:
        return LOGIN_PAGE

    return DASHBOARD_PAGE

# ── 데이터 저장 ────────────────────────────────────
@app.post("/add/expense")
async def add_expense(date: str = Form(...), store: str = Form(...), amount: int = Form(...), category: str = Form(...), session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    doc = {
        "user_id": user["_id"],
        "date": date,
        "store_name": store,
        "amount": amount,
        "category": category,
        "created_at": datetime.now().isoformat()
    }
    await db.expenses.insert_one(doc)
    return {"status": "success"}

@app.get("/api/expenses")
async def get_expenses(month: Optional[str] = None, session: Optional[str] = Cookie(None)):
    """사용자 지출 목록 조회 (월별)"""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    if not month:
        month = datetime.now().strftime("%Y-%m")

    expenses = []
    async for doc in db.expenses.find({
        "user_id": user["_id"],
        "date": {"$regex": f"^{month}"}
    }).sort("date", 1):
        doc["_id"] = str(doc["_id"])
        doc.pop("user_id", None)  # ObjectId는 JSON 직렬화 불가 + 프론트 미사용
        expenses.append(doc)

    return {"expenses": expenses}

@app.get("/api/expenses/by-date")
async def get_expenses_by_date(date: str, session: Optional[str] = Cookie(None)):
    """특정 날짜의 지출 목록 조회"""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    expenses = []
    async for doc in db.expenses.find({
        "user_id": user["_id"],
        "date": date
    }).sort("created_at", -1):
        doc["_id"] = str(doc["_id"])
        doc.pop("user_id", None)  # ObjectId는 JSON 직렬화 불가 + 프론트 미사용
        expenses.append(doc)

    return {"expenses": expenses}

# ── 분석 (기간별 집계) ─────────────────────────────
def get_period_range(period: str, offset: int = 0) -> tuple[str, str, str]:
    """기간+오프셋 → (시작일, 종료일, 표시라벨). offset 0=현재, -1=직전, +1=다음.
    날짜가 문자열이라 사전순 비교 가능."""
    today = datetime.now()
    if period == "week":
        # 이번 주 월요일 ~ 일요일 (offset 주 단위 이동)
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        sunday = monday + timedelta(days=6)
        label = f"{monday.month}월 {monday.day}일 ~ {sunday.month}월 {sunday.day}일"
        return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"), label
    if period == "year":
        year = today.year + offset
        return f"{year}-01-01", f"{year}-12-31", f"{year}년"
    # 기본: 달 단위 (offset 개월 이동, 연도 넘김 처리)
    month_index = today.year * 12 + (today.month - 1) + offset
    year, month = divmod(month_index, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}", f"{year}년 {month}월"

@app.get("/api/analysis")
async def get_analysis(period: str = "month", offset: int = 0, session: Optional[str] = Cookie(None)):
    """기간별(month/week/year) 카테고리 집계 + 지출 목록. offset으로 이전/다음 기간 이동."""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    if period not in ("month", "week", "year"):
        period = "month"

    start, end, label = get_period_range(period, offset)

    expenses = []
    category_totals = {}
    total = 0
    async for doc in db.expenses.find({
        "user_id": user["_id"],
        "date": {"$gte": start, "$lte": end}
    }).sort("date", -1):
        doc["_id"] = str(doc["_id"])
        doc.pop("user_id", None)  # ObjectId는 JSON 직렬화 불가 + 프론트 미사용
        expenses.append(doc)
        amount = doc.get("amount", 0)
        total += amount
        cat = doc.get("category", "기타")
        category_totals[cat] = category_totals.get(cat, 0) + amount

    by_category = sorted(
        [{"category": c, "amount": a} for c, a in category_totals.items()],
        key=lambda x: x["amount"],
        reverse=True
    )

    return {
        "period": period,
        "offset": offset,
        "label": label,
        "start": start,
        "end": end,
        "total": total,
        "by_category": by_category,
        "expenses": expenses
    }

# ── 카테고리 자동분류 (무료 로컬: 과거 내역 학습 → 키워드 → 기본값) ──
# 처음 보는 가게 대비 최소 키워드 힌트. 사용자 카테고리에 존재할 때만 매핑됨.
CATEGORY_KEYWORD_HINTS = {
    "카페": ["카페", "커피", "스타벅스", "스벅", "이디야", "투썸", "메가", "빽다방", "컴포즈", "coffee"],
    "식사": ["식당", "김밥", "분식", "샐러디", "맥도날드", "버거", "롯데리아", "서브웨이", "국밥",
             "치킨", "피자", "백반", "한식", "일식", "중식", "떡볶이", "배달", "food"],
    "교통": ["택시", "버스", "지하철", "교통", "주유", "기차", "ktx", "카카오t"],
    "쇼핑": ["마트", "쿠팡", "편의점", "gs25", "cu", "세븐일레븐", "이마트", "올리브영", "다이소"],
}

async def classify_expense(user: dict, store_name: str) -> str:
    """가게명 → 사용자 카테고리 중 하나로 자동 분류.
    ① 과거 내역에서 같은/유사 가게명의 최빈 카테고리 (개인화 학습)
    ② 키워드 힌트 (사용자 카테고리에 존재할 때만)
    ③ 기본값(첫 카테고리)"""
    categories = user.get("categories", DEFAULT_CATEGORIES)
    store = (store_name or "").strip()
    if not store or not categories:
        return categories[0] if categories else "기타"

    # ① 과거 내역 조회 → 최빈 카테고리
    counts = {}
    async for doc in db.expenses.find({"user_id": user["_id"]}):
        past_store = (doc.get("store_name") or "").strip()
        cat = doc.get("category")
        if not past_store or not cat or cat not in categories:
            continue
        if past_store == store or past_store in store or store in past_store:
            counts[cat] = counts.get(cat, 0) + 1
    if counts:
        return max(counts, key=counts.get)

    # ② 키워드 힌트
    low = store.lower()
    for cat, kws in CATEGORY_KEYWORD_HINTS.items():
        if cat in categories and any(k in low for k in kws):
            return cat

    # ③ 기본값
    return categories[0]

# ── 챗봇 ──────────────────────────────────────────
@app.post("/chat")
async def chat(message: str = Form(...), session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    text = message.strip()

    # 질문 키워드 (지출 조회) — 저장 의도와 구분
    is_query = any(kw in text for kw in ["얼마", "지출", "썼", "쓴", "총", "합계"])

    # 저장 의도: "6/30 샐러디 9900원" 처럼 금액+가게가 파싱되면 확인 카드 반환
    if not is_query:
        parsed = parse_natural_language(text)
        if parsed["amount"] is not None and parsed["store_name"]:
            category = await classify_expense(user, parsed["store_name"])
            return {
                "type": "confirm",
                "expense": {
                    "date": parsed["date"],
                    "store": parsed["store_name"],
                    "amount": parsed["amount"],
                    "category": category,
                },
                "categories": user.get("categories", DEFAULT_CATEGORIES),
                "response": f"이렇게 저장할까요?",
            }

    # 지출/금액 관련 질문 처리
    if is_query:
        # 기간 파악 (기본: 이번달)
        if any(w in text for w in ["지난달", "저번달", "전달"]):
            period, offset, when = "month", -1, "지난달"
        elif any(w in text for w in ["지난주", "저번주"]):
            period, offset, when = "week", -1, "지난주"
        elif any(w in text for w in ["이번주", "이번 주"]):
            period, offset, when = "week", 0, "이번 주"
        elif any(w in text for w in ["작년", "지난해"]):
            period, offset, when = "year", -1, "작년"
        elif any(w in text for w in ["올해", "이번해", "이번 해"]):
            period, offset, when = "year", 0, "올해"
        else:
            period, offset, when = "month", 0, "이번달"

        start, end, _ = get_period_range(period, offset)
        query = {"user_id": user["_id"], "date": {"$gte": start, "$lte": end}}

        # 메시지에 사용자 카테고리가 있으면 해당 카테고리만 집계
        matched_cat = next((c for c in user.get("categories", []) if c in text), None)
        if matched_cat:
            query["category"] = matched_cat

        expenses = await db.expenses.find(query).to_list(None)
        total = sum(e.get("amount", 0) for e in expenses)

        cat_str = f" '{matched_cat}'" if matched_cat else ""
        return {"type": "message", "response": f"{when}{cat_str} 지출: {total:,}원 ({len(expenses)}건)"}

    return {"type": "message", "response": "지출을 물어보거나(\"이번달 얼마 썼어?\") 저장할 내역을 입력하세요(\"6/30 샐러디 9900원\")."}

# HTML 페이지들
LOGIN_PAGE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Receiptly - 로그인</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .container {
            background: white;
            border-radius: 12px;
            padding: 40px;
            width: 90%;
            max-width: 400px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }
        h1 { font-size: 32px; margin-bottom: 30px; text-align: center; color: #333; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; color: #666; font-weight: 500; }
        input { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
        button { width: 100%; padding: 12px; background: #4A90D9; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: 500; margin-top: 10px; }
        button:hover { background: #357ABD; }
        .toggle { text-align: center; margin-top: 15px; font-size: 14px; color: #666; }
        .toggle a { color: #4A90D9; cursor: pointer; }
        .form-section { display: none; }
        .form-section.active { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🧾 Receiptly</h1>

        <div id="login" class="form-section active">
            <form onsubmit="handleLogin(event)">
                <div class="form-group">
                    <label>아이디</label>
                    <input type="text" name="username" required />
                </div>
                <div class="form-group">
                    <label>비밀번호</label>
                    <input type="password" name="password" required />
                </div>
                <button type="submit">로그인</button>
            </form>
            <div class="toggle">
                계정이 없으신가요? <a onclick="toggleForm()">회원가입</a>
            </div>
        </div>

        <div id="signup" class="form-section">
            <form onsubmit="handleSignup(event)">
                <div class="form-group">
                    <label>아이디</label>
                    <input type="text" id="signup-username" name="username" placeholder="4자 이상" required onblur="checkUsername()" />
                    <small id="username-feedback" style="font-size: 12px; color: #999;"></small>
                </div>
                <div class="form-group">
                    <label>비밀번호</label>
                    <input type="password" id="signup-password" name="password" placeholder="8자+영어+숫자+특수문자(!,#,$)" required onkeyup="checkPassword()" />
                    <small id="password-feedback" style="font-size: 12px; color: #999;"></small>
                    <div id="password-checks" style="font-size: 11px; margin-top: 8px;">
                        <div id="check-length" style="color: #999;">✗ 8자 이상</div>
                        <div id="check-letter" style="color: #999;">✗ 영어 포함</div>
                        <div id="check-number" style="color: #999;">✗ 숫자 포함</div>
                        <div id="check-special" style="color: #999;">✗ 특수문자(!,#,$) 포함</div>
                    </div>
                </div>
                <div class="form-group">
                    <label>비밀번호 확인</label>
                    <input type="password" id="signup-password2" name="password2" required onkeyup="checkPasswordMatch()" />
                    <small id="password-match-feedback" style="font-size: 12px; color: #999;"></small>
                </div>
                <button type="submit" id="signup-btn" disabled>가입하기</button>
            </form>
            <div class="toggle">
                이미 계정이 있으신가요? <a onclick="toggleForm()">로그인</a>
            </div>
        </div>
    </div>

    <script>
    function toggleForm() {
        document.getElementById('login').classList.toggle('active');
        document.getElementById('signup').classList.toggle('active');
    }

    // 아이디 중복 확인
    async function checkUsername() {
        const username = document.getElementById('signup-username').value;
        const feedback = document.getElementById('username-feedback');

        if (!username) {
            feedback.textContent = '';
            return;
        }

        const form = new FormData();
        form.append('username', username);

        const response = await fetch('/auth/check-username', {method: 'POST', body: form});
        const result = await response.json();

        if (result.available) {
            feedback.textContent = '✓ ' + result.message;
            feedback.style.color = '#4CAF50';
        } else {
            feedback.textContent = '✗ ' + result.message;
            feedback.style.color = '#f44336';
        }

        updateSignupButton();
    }

    // 비밀번호 검증
    function checkPassword() {
        const password = document.getElementById('signup-password').value;

        const checks = {
            'check-length': password.length >= 8,
            'check-letter': /[a-zA-Z]/.test(password),
            'check-number': /[0-9]/.test(password),
            'check-special': /[!#$]/.test(password)
        };

        for (const [id, passed] of Object.entries(checks)) {
            const elem = document.getElementById(id);
            if (passed) {
                elem.style.color = '#4CAF50';
                elem.textContent = elem.textContent.replace('✗', '✓');
            } else {
                elem.style.color = '#999';
                elem.textContent = elem.textContent.replace('✓', '✗');
            }
        }

        checkPasswordMatch();
        updateSignupButton();
    }

    // 비밀번호 일치 확인
    function checkPasswordMatch() {
        const pwd = document.getElementById('signup-password').value;
        const pwd2 = document.getElementById('signup-password2').value;
        const feedback = document.getElementById('password-match-feedback');

        if (!pwd2) {
            feedback.textContent = '';
            return;
        }

        if (pwd === pwd2) {
            feedback.textContent = '✓ 비밀번호가 일치합니다';
            feedback.style.color = '#4CAF50';
        } else {
            feedback.textContent = '✗ 비밀번호가 일치하지 않습니다';
            feedback.style.color = '#f44336';
        }

        updateSignupButton();
    }

    // 가입 버튼 활성화 여부
    async function updateSignupButton() {
        const username = document.getElementById('signup-username').value;
        const password = document.getElementById('signup-password').value;
        const password2 = document.getElementById('signup-password2').value;
        const usernameFeedback = document.getElementById('username-feedback').style.color === 'rgb(76, 175, 80)';
        const passwordValid = password.length >= 8 && /[a-zA-Z]/.test(password) && /[0-9]/.test(password) && /[!#$]/.test(password);
        const passwordMatch = password === password2 && password2;

        const btn = document.getElementById('signup-btn');
        btn.disabled = !(usernameFeedback && passwordValid && passwordMatch);
    }

    async function handleLogin(e) {
        e.preventDefault();
        const data = new FormData(e.target);
        const response = await fetch('/auth/login', {method: 'POST', body: data});
        const result = await response.json();
        if (result.status === 'success') {
            location.href = '/';
        } else {
            alert(result.error);
        }
    }

    async function handleSignup(e) {
        e.preventDefault();
        const data = new FormData(e.target);
        const response = await fetch('/auth/signup', {method: 'POST', body: data});
        const result = await response.json();
        if (result.status === 'success') {
            location.href = '/';
        } else {
            alert(result.error);
        }
    }
    </script>
</body>
</html>"""

DASHBOARD_PAGE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Receiptly - 가계부</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; }
        .container { display: grid; grid-template-columns: 1.5fr 320px; gap: 20px; padding: 20px; max-width: 1600px; margin: 0 auto; }
        .main { background: white; border-radius: 12px; padding: 30px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .sidebar { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); height: fit-content; }

        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; border-bottom: 2px solid #eee; padding-bottom: 20px; }
        .header h1 { font-size: 32px; }
        .header button { padding: 8px 16px; width: auto; }

        .tabs { display: flex; gap: 10px; margin-bottom: 20px; border-bottom: 2px solid #eee; }
        .tab { padding: 12px 20px; cursor: pointer; border: none; background: none; font-size: 16px; color: #666; border-bottom: 3px solid transparent; }
        .tab.active { color: #4A90D9; border-bottom-color: #4A90D9; }

        .tab-content { display: none; }
        .tab-content.active { display: block; }

        .calendar-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .calendar-header h3 { flex: 1; text-align: center; }
        .calendar-header button { padding: 8px 12px; width: auto; }

        .calendar { display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; margin-bottom: 30px; }
        .calendar-label { text-align: center; font-weight: 600; padding: 10px 0; font-size: 12px; color: #999; }
        .calendar-day { padding: 12px 8px; background: #f9f9f9; border-radius: 6px; text-align: center; cursor: pointer; border: 1px solid #ddd; min-height: 80px; display: flex; flex-direction: column; justify-content: space-between; font-size: 13px; }
        .calendar-day:hover { background: #e8f4f8; border-color: #4A90D9; }
        .calendar-day.today { background: #fff3cd; border-color: #ffc107; }
        .calendar-day.selected { background: #4A90D9; color: white; border-color: #4A90D9; }
        .calendar-day .date { font-weight: 600; }
        .calendar-day .amount { font-size: 11px; color: #666; margin-top: 4px; }
        .calendar-day.selected .amount { color: #e8f4f8; }

        .stats { background: #f9f9f9; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
        .stats h4 { margin-bottom: 15px; }
        .stat-item { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #eee; }
        .stat-item:last-child { border-bottom: none; }
        .stat-item .category { font-weight: 500; }
        .stat-item .amount { color: #4A90D9; font-weight: 600; }

        .period-buttons { display: flex; gap: 10px; margin-bottom: 20px; }
        .period-buttons button { flex: 1; padding: 8px; background: #f0f0f0; border: none; border-radius: 6px; cursor: pointer; }
        .period-buttons button.active { background: #4A90D9; color: white; }

        table { width: 100%; border-collapse: collapse; }
        table th { background: #f9f9f9; padding: 12px; text-align: left; border-bottom: 2px solid #eee; font-weight: 600; }
        table td { padding: 12px; border-bottom: 1px solid #eee; }
        table tr:hover { background: #f5f5f5; }

        input, select, button { width: 100%; padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
        button { background: #4A90D9; color: white; border: none; cursor: pointer; font-weight: 500; }
        button:hover { background: #357ABD; }
        button:disabled { background: #ccc; cursor: not-allowed; }

        .chatbot { border: 1px solid #ddd; border-radius: 8px; display: flex; flex-direction: column; height: 500px; }
        .chat-messages { flex: 1; overflow-y: auto; padding: 15px; font-size: 13px; }
        .chat-message { margin: 8px 0; padding: 10px; border-radius: 6px; word-wrap: break-word; }
        .chat-message.bot { background: #E8F4F8; }
        .chat-message.user { background: #4A90D933; text-align: right; }
        .chat-input { padding: 10px; border-top: 1px solid #ddd; display: flex; gap: 5px; }
        .chat-input input { margin: 0; flex: 1; }
        .chat-input button { margin: 0; width: 40px; padding: 8px; }

        .sidebar h3 { margin-bottom: 15px; font-size: 16px; }
        .sidebar hr { margin: 20px 0; }

        .selected-date-info { background: #f9f9f9; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
        .selected-date-info h4 { margin-bottom: 10px; color: #333; }
        .selected-date-info .date-label { font-size: 12px; color: #999; }

        @media (max-width: 1000px) {
            .container { grid-template-columns: 1fr; }
            .calendar-day { min-height: 60px; }
        }

        /* 비차단 토스트 (alert 대체) */
        #toast-container { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 2000; display: flex; flex-direction: column; gap: 8px; align-items: center; pointer-events: none; }
        .toast { background: #333; color: white; padding: 12px 20px; border-radius: 8px; font-size: 14px; box-shadow: 0 4px 12px rgba(0,0,0,0.25); opacity: 0; transform: translateY(-10px); transition: opacity 0.25s, transform 0.25s; max-width: 90vw; }
        .toast.show { opacity: 1; transform: translateY(0); }
        .toast.success { background: #4CAF50; }
        .toast.error { background: #f44336; }
    </style>
</head>
<body>
    <div id="toast-container"></div>
    <div class="container">
        <div class="main">
            <div class="header">
                <h1>🧾 Receiptly</h1>
                <button onclick="logout()">로그아웃</button>
            </div>

            <div class="tabs">
                <button class="tab active" onclick="switchTab('calendar')">📅 달력</button>
                <button class="tab" onclick="switchTab('analysis')">📊 분석</button>
            </div>

            <!-- 달력 탭 -->
            <div id="calendar" class="tab-content active">
                <div class="calendar-header">
                    <button onclick="prevMonth()">←</button>
                    <h3 id="monthTitle">2024년 6월</h3>
                    <button onclick="nextMonth()">→</button>
                </div>

                <div style="display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; margin-bottom: 20px;">
                    <div class="calendar-label">일</div>
                    <div class="calendar-label">월</div>
                    <div class="calendar-label">화</div>
                    <div class="calendar-label">수</div>
                    <div class="calendar-label">목</div>
                    <div class="calendar-label">금</div>
                    <div class="calendar-label">토</div>
                </div>
                <div id="calendar-grid" class="calendar"></div>

                <div id="selected-date-section" style="display: none;">
                    <div class="stats" id="selected-date-stats">
                        <h4>📅 지출 내역</h4>
                        <div class="stat-item">
                            <span>지출이 없습니다</span>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 분석 탭 -->
            <div id="analysis" class="tab-content">
                <div class="period-buttons">
                    <button class="active" onclick="setPeriod('month')">달별</button>
                    <button onclick="setPeriod('week')">주별</button>
                    <button onclick="setPeriod('year')">연별</button>
                </div>

                <div class="period-nav" style="display: flex; align-items: center; justify-content: center; gap: 15px; margin-bottom: 20px;">
                    <button onclick="movePeriod(-1)" style="width: auto; padding: 8px 16px; margin: 0;">←</button>
                    <span id="period-label" style="font-weight: 600; font-size: 16px; min-width: 180px; text-align: center;"></span>
                    <button onclick="movePeriod(1)" style="width: auto; padding: 8px 16px; margin: 0;">→</button>
                </div>

                <div class="stats" id="analysis-stats">
                    <h4>카테고리별 지출</h4>
                    <div class="stat-item">
                        <span>데이터 로딩 중...</span>
                    </div>
                </div>

                <table>
                    <thead>
                        <tr>
                            <th>날짜</th>
                            <th>가게</th>
                            <th style="text-align: right;">금액</th>
                            <th>카테고리</th>
                        </tr>
                    </thead>
                    <tbody id="expense-table">
                        <tr><td colspan="4" style="text-align: center; color: #999;">지출 내역이 없습니다</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 사이드바 -->
        <div class="sidebar">
            <h3>✏️ 지출 입력</h3>
            <input type="text" id="store" placeholder="가게명" />
            <input type="number" id="amount" placeholder="금액" />
            <input type="date" id="date" />
            <div style="display: flex; gap: 5px;">
                <select id="category" style="flex: 1;">
                    <option>카페</option>
                    <option>식사</option>
                    <option>데이트</option>
                    <option>고정지출</option>
                </select>
                <button onclick="openCategoryModal()" style="width: 40px; padding: 10px; margin: 8px 0;">⚙️</button>
            </div>
            <button onclick="addExpense()">저장</button>

            <hr />

            <h3>💬 챗봇</h3>
            <div class="chatbot">
                <div class="chat-messages" id="messages">
                    <div class="chat-message bot">안녕하세요! 지출을 물어보거나("이번달 얼마 썼어?") 바로 입력해 저장할 수 있어요.<br>예: "6/30 샐러디 9900원"</div>
                </div>
                <div class="chat-input">
                    <input type="text" id="chat-input" placeholder="예: 6/30 샐러디 9900원" onkeypress="if(event.key=='Enter') sendChat()" />
                    <button onclick="sendChat()">→</button>
                </div>
            </div>
        </div>
    </div>

    <!-- 카테고리 관리 모달 -->
    <div id="categoryModal" style="display: none !important; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center;">
        <div style="background: white; border-radius: 12px; padding: 30px; width: 90%; max-width: 400px; box-shadow: 0 10px 40px rgba(0,0,0,0.2);">
            <h2>📂 카테고리 관리</h2>

            <div style="margin: 20px 0;">
                <h4 style="margin-bottom: 10px;">현재 카테고리</h4>
                <div id="categoryList" style="max-height: 250px; overflow-y: auto;"></div>
            </div>

            <h4 style="margin-top: 20px; margin-bottom: 10px;">새 카테고리 추가</h4>
            <div style="display: flex; gap: 5px;">
                <input type="text" id="newCategoryName" placeholder="카테고리명" style="flex: 1;" />
                <button onclick="addNewCategory()" style="width: 60px;">추가</button>
            </div>

            <button onclick="closeCategoryModal()" style="margin-top: 20px; background: #999;">닫기</button>
        </div>
    </div>

    <script>
    let currentMonth = new Date();
    let selectedDate = null;
    let monthExpenses = [];

    // 비차단 토스트 (탭을 멈추는 alert 대체). type: '' | 'success' | 'error'
    function showToast(message, type = '') {
        const container = document.getElementById('toast-container');
        if (!container) return;
        const toast = document.createElement('div');
        toast.className = 'toast' + (type ? ' ' + type : '');
        toast.textContent = message;
        container.appendChild(toast);
        requestAnimationFrame(() => toast.classList.add('show'));
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }, 2500);
    }

    // 월별 지출 데이터 로드
    async function loadExpensesAndRender() {
        try {
            const year = currentMonth.getFullYear();
            const month = String(currentMonth.getMonth() + 1).padStart(2, '0');
            const monthStr = year + '-' + month;

            console.log('📅 지출 데이터 로드: ' + monthStr);

            const response = await fetch('/api/expenses?month=' + monthStr);
            if (!response.ok) {
                throw new Error('API 응답 실패: ' + response.status);
            }
            const data = await response.json();
            monthExpenses = data.expenses || [];

            console.log('✅ 로드된 지출 기록: ' + monthExpenses.length + '개');

            // 달력 렌더링
            if (document.getElementById('calendar-grid')) {
                renderCalendar();
                console.log('✅ 달력 렌더링 완료');
            } else {
                console.warn('⚠️ calendar-grid 요소를 찾을 수 없음');
            }
        } catch (e) {
            console.error('지출 데이터 로드 오류:', e);
            monthExpenses = [];
            renderCalendar();
        }
    }

    function switchTab(tab) {
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
        document.getElementById(tab).classList.add('active');
        event.target.classList.add('active');
        if (tab === 'calendar') renderCalendar();
        else loadAnalysis();
    }

    function prevMonth() {
        currentMonth.setMonth(currentMonth.getMonth() - 1);
        loadExpensesAndRender();
    }

    function nextMonth() {
        currentMonth.setMonth(currentMonth.getMonth() + 1);
        loadExpensesAndRender();
    }

    function renderCalendar() {
        const year = currentMonth.getFullYear();
        const month = currentMonth.getMonth();
        const monthNames = ['1월', '2월', '3월', '4월', '5월', '6월', '7월', '8월', '9월', '10월', '11월', '12월'];

        console.log('🎨 달력 렌더링 시작: ' + year + '년 ' + monthNames[month] + ' (지출: ' + monthExpenses.length + '개)');

        document.getElementById('monthTitle').textContent = year + '년 ' + monthNames[month];

        const grid = document.getElementById('calendar-grid');
        grid.innerHTML = '';

        const firstDay = new Date(year, month, 1).getDay();
        const daysInMonth = new Date(year, month + 1, 0).getDate();
        const today = new Date();

        for (let i = 0; i < firstDay; i++) {
            const empty = document.createElement('div');
            grid.appendChild(empty);
        }

        let daysWithExpenses = 0;

        for (let day = 1; day <= daysInMonth; day++) {
            const dateStr = year + '-' + String(month + 1).padStart(2, '0') + '-' + String(day).padStart(2, '0');
            const dayDiv = document.createElement('div');
            dayDiv.className = 'calendar-day';

            const isToday = dateStr === today.toISOString().split('T')[0];
            if (isToday) dayDiv.classList.add('today');

            // 해당 날짜의 지출 정보 추출
            const dayExpenses = monthExpenses.filter(e => e.date === dateStr);
            if (dayExpenses.length > 0) {
                daysWithExpenses++;
            }

            const categoryTotals = {};
            dayExpenses.forEach(e => {
                if (!categoryTotals[e.category]) {
                    categoryTotals[e.category] = 0;
                }
                categoryTotals[e.category] += e.amount;
            });

            // HTML 구성
            let html = '<div class="date">' + day + '</div>';
            if (dayExpenses.length > 0) {
                html += '<div style="font-size: 11px; margin-top: 4px;">';
                for (const [cat, total] of Object.entries(categoryTotals)) {
                    html += '<div style="color: #666; margin-bottom: 2px;">' + cat + ' ' + total.toLocaleString() + '원</div>';
                }
                html += '</div>';
            }

            dayDiv.innerHTML = html;
            dayDiv.onclick = () => selectDate(dateStr);
            grid.appendChild(dayDiv);
        }

        console.log('✅ 달력 렌더링 완료 - ' + daysWithExpenses + '개 날짜에 지출 표시');
    }

    function selectDate(dateStr) {
        selectedDate = dateStr;

        // 달력 선택 상태 업데이트
        document.querySelectorAll('.calendar-day').forEach(d => d.classList.remove('selected'));
        if (event && event.target) {
            const dayEl = event.target.closest('.calendar-day');
            if (dayEl) dayEl.classList.add('selected');
        }

        // 해당 날짜의 지출 내역만 표시
        showDateExpenses(dateStr);
    }

    async function showDateExpenses(dateStr) {
        try {
            console.log('📋 조회: ' + dateStr);

            const response = await fetch('/api/expenses/by-date?date=' + dateStr);
            if (!response.ok) {
                throw new Error('API 응답 실패: ' + response.status);
            }

            const data = await response.json();
            const expenses = data.expenses || [];

            console.log('✅ ' + dateStr + ' 지출: ' + expenses.length + '개');

            const statsDiv = document.getElementById('selected-date-stats');
            if (expenses.length === 0) {
                statsDiv.innerHTML = '<h4>📅 ' + dateStr + ' 지출 내역</h4><div class="stat-item"><span>이 날짜에 지출이 없습니다</span></div>';
            } else {
                const total = expenses.reduce((sum, e) => sum + e.amount, 0);
                const html = '<h4>📅 ' + dateStr + ' 지출 내역 (' + expenses.length + '건)</h4>' +
                    expenses.map(e => `<div class="stat-item"><span class="category">${e.store_name} <span style="font-size: 12px; color: #999;">(${e.category})</span></span><span class="amount">${e.amount.toLocaleString()}원</span></div>`).join('') +
                    `<div class="stat-item" style="border-top: 2px solid #ddd; padding-top: 10px; margin-top: 10px; font-weight: 600;"><span>합계</span><span style="color: #4A90D9;">${total.toLocaleString()}원</span></div>`;
                statsDiv.innerHTML = html;
            }
            document.getElementById('selected-date-section').style.display = 'block';
        } catch (e) {
            console.error('지출 조회 오류:', e);
            showToast('지출 내역을 불러올 수 없습니다', 'error');
        }
    }

    let currentPeriod = 'month';
    let periodOffset = 0;

    function setPeriod(period) {
        currentPeriod = period;
        periodOffset = 0;  // 기간 종류 바꾸면 현재 기간으로 리셋
        document.querySelectorAll('.period-buttons button').forEach(b => b.classList.remove('active'));
        event.target.classList.add('active');
        loadAnalysis();
    }

    function movePeriod(delta) {
        periodOffset += delta;  // -1: 이전 기간, +1: 다음 기간
        loadAnalysis();
    }

    async function loadAnalysis() {
        const statsDiv = document.getElementById('analysis-stats');
        const tableBody = document.getElementById('expense-table');
        const labelEl = document.getElementById('period-label');

        try {
            const response = await fetch('/api/analysis?period=' + currentPeriod + '&offset=' + periodOffset);
            if (!response.ok) {
                throw new Error('API 응답 실패: ' + response.status);
            }
            const data = await response.json();
            const byCategory = data.by_category || [];
            const expenses = data.expenses || [];
            const total = data.total || 0;
            const periodLabel = data.label || '';

            // 기간 라벨 표시 (예: 2026년 7월, 6월 29일 ~ 7월 5일)
            if (labelEl) labelEl.textContent = periodLabel;

            // 카테고리별 집계
            if (byCategory.length === 0) {
                statsDiv.innerHTML = '<h4>카테고리별 지출 (' + periodLabel + ')</h4><div class="stat-item"><span>지출 내역이 없습니다</span></div>';
            } else {
                const rows = byCategory.map(c =>
                    '<div class="stat-item"><span class="category">' + c.category + '</span><span class="amount">' + c.amount.toLocaleString() + '원</span></div>'
                ).join('');
                statsDiv.innerHTML = '<h4>카테고리별 지출 (' + periodLabel + ')</h4>' + rows +
                    '<div class="stat-item" style="border-top: 2px solid #ddd; padding-top: 10px; margin-top: 10px; font-weight: 600;"><span>합계</span><span style="color: #4A90D9;">' + total.toLocaleString() + '원</span></div>';
            }

            // 지출 목록 테이블
            if (expenses.length === 0) {
                tableBody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #999;">지출 내역이 없습니다</td></tr>';
            } else {
                tableBody.innerHTML = expenses.map(e =>
                    '<tr><td>' + e.date + '</td><td>' + e.store_name + '</td><td style="text-align: right;">' + e.amount.toLocaleString() + '원</td><td>' + e.category + '</td></tr>'
                ).join('');
            }
        } catch (e) {
            console.error('분석 데이터 로드 오류:', e);
            statsDiv.innerHTML = '<h4>카테고리별 지출</h4><div class="stat-item"><span>데이터를 불러올 수 없습니다</span></div>';
            tableBody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #999;">데이터를 불러올 수 없습니다</td></tr>';
        }
    }

    async function addExpense() {
        const store = document.getElementById('store').value;
        const amount = document.getElementById('amount').value;
        const date = document.getElementById('date').value;
        const category = document.getElementById('category').value;

        if (!store || !amount || !date) {
            showToast('모든 항목을 입력해주세요', 'error');
            return;
        }

        const form = new FormData();
        form.append('date', date);
        form.append('store', store);
        form.append('amount', parseInt(amount));
        form.append('category', category);

        try {
            const response = await fetch('/add/expense', {method: 'POST', body: form});
            const result = await response.json();
            if (result.status === 'success') {
                document.getElementById('store').value = '';
                document.getElementById('amount').value = '';

                // 입력한 날짜의 달로 이동
                const inputDate = new Date(date);
                currentMonth = new Date(inputDate.getFullYear(), inputDate.getMonth(), 1);

                await loadExpensesAndRender();

                // 달력 탭으로 자동 전환
                document.getElementById('calendar').classList.add('active');
                document.getElementById('analysis').classList.remove('active');
                document.querySelectorAll('.tab').forEach((btn, idx) => {
                    if (idx === 0) btn.classList.add('active');
                    else btn.classList.remove('active');
                });

                showToast('저장 완료!', 'success');
            } else {
                showToast(result.error || '저장 실패', 'error');
            }
        } catch (e) {
            console.error('지출 저장 오류:', e);
            showToast('오류가 발생했습니다', 'error');
        }
    }

    let chatCardSeq = 0;
    const pendingExpenses = {};

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    async function sendChat() {
        const inputEl = document.getElementById('chat-input');
        const input = inputEl.value.trim();
        if (!input) return;

        const messages = document.getElementById('messages');
        messages.innerHTML += '<div class="chat-message user">' + escapeHtml(input) + '</div>';
        inputEl.value = '';
        messages.scrollTop = messages.scrollHeight;

        try {
            const response = await fetch('/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'message=' + encodeURIComponent(input)
            });
            const data = await response.json();

            if (data.type === 'confirm' && data.expense) {
                renderConfirmCard(data.expense, data.categories || []);
            } else {
                messages.innerHTML += '<div class="chat-message bot">' + (data.response || '') + '</div>';
            }
        } catch (e) {
            messages.innerHTML += '<div class="chat-message bot">오류가 발생했어요. 다시 시도해주세요.</div>';
        }
        messages.scrollTop = messages.scrollHeight;
    }

    // 파싱 결과 확인 카드 (저장 전 사용자 확인/수정)
    function renderConfirmCard(expense, categories) {
        const messages = document.getElementById('messages');
        const n = chatCardSeq++;
        const id = 'cc' + n;
        pendingExpenses[n] = expense;

        const opts = categories.map(c =>
            '<option value="' + escapeHtml(c) + '"' + (c === expense.category ? ' selected' : '') + '>' + escapeHtml(c) + '</option>'
        ).join('');

        messages.innerHTML +=
            '<div class="chat-message bot" id="' + id + '" style="text-align:left;">' +
                '<div style="font-weight:600; margin-bottom:6px;">이렇게 저장할까요?</div>' +
                '<div style="font-size:13px; line-height:1.7;">' +
                    '📅 ' + escapeHtml(expense.date) + '<br>' +
                    '🏪 ' + escapeHtml(expense.store) + '<br>' +
                    '💰 ' + Number(expense.amount).toLocaleString() + '원' +
                '</div>' +
                '<select id="' + id + '-cat" style="width:100%; margin:8px 0 0; padding:6px; font-size:13px;">' + opts + '</select>' +
                '<div style="display:flex; gap:5px; margin-top:8px;">' +
                    '<button onclick="confirmSave(' + n + ')" style="flex:1; margin:0; padding:8px; font-size:13px;">저장</button>' +
                    '<button onclick="cancelSave(' + n + ')" style="flex:1; margin:0; padding:8px; font-size:13px; background:#999;">취소</button>' +
                '</div>' +
            '</div>';
        messages.scrollTop = messages.scrollHeight;
    }

    async function confirmSave(n) {
        const expense = pendingExpenses[n];
        if (!expense) return;
        const id = 'cc' + n;
        const catSel = document.getElementById(id + '-cat');
        const category = catSel ? catSel.value : expense.category;

        const form = new FormData();
        form.append('date', expense.date);
        form.append('store', expense.store);
        form.append('amount', parseInt(expense.amount));
        form.append('category', category);

        try {
            const res = await fetch('/add/expense', {method: 'POST', body: form});
            const result = await res.json();
            const card = document.getElementById(id);
            if (result.status === 'success') {
                if (card) card.outerHTML = '<div class="chat-message bot">✅ 저장 완료: ' +
                    escapeHtml(expense.date) + ' · ' + escapeHtml(expense.store) + ' · ' +
                    Number(expense.amount).toLocaleString() + '원 · ' + escapeHtml(category) + '</div>';
                delete pendingExpenses[n];
                // 저장한 날짜의 달로 달력 갱신
                const d = new Date(expense.date);
                currentMonth = new Date(d.getFullYear(), d.getMonth(), 1);
                loadExpensesAndRender();
            } else {
                showToast(result.error || '저장 실패', 'error');
            }
        } catch (e) {
            showToast('저장 중 오류가 발생했습니다', 'error');
        }
        const messages = document.getElementById('messages');
        messages.scrollTop = messages.scrollHeight;
    }

    function cancelSave(n) {
        delete pendingExpenses[n];
        const card = document.getElementById('cc' + n);
        if (card) card.outerHTML = '<div class="chat-message bot">취소했어요.</div>';
    }

    async function logout() {
        await fetch('/auth/logout', {method: 'POST'});
        location.href = '/';
    }

    // 카테고리 관리 함수들
    async function loadCategories() {
        try {
            const response = await fetch('/api/categories');
            const data = await response.json();
            if (data.categories) {
                updateSelectOptions(data.categories);
                if (document.getElementById('categoryList')) {
                    renderCategoryList(data.categories);
                }
            }
        } catch (e) {
            console.log('카테고리 로드 중 오류:', e);
        }
    }

    function renderCategoryList(categories) {
        const list = document.getElementById('categoryList');
        list.innerHTML = categories.map((cat, idx) => `
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px; background: #f9f9f9; border-radius: 6px; margin-bottom: 8px;">
                <span id="cat-display-${idx}" style="flex: 1;">${cat}</span>
                <div id="cat-edit-${idx}" style="display: none; flex: 1; display: flex; gap: 5px;">
                    <input type="text" id="cat-input-${idx}" value="${cat}" style="flex: 1; padding: 5px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px;" />
                    <button onclick="saveEdit(${idx})" style="padding: 5px 10px; width: auto; background: #4CAF50; font-size: 12px;">✓</button>
                    <button onclick="cancelEdit(${idx})" style="padding: 5px 10px; width: auto; background: #999; font-size: 12px;">✕</button>
                </div>
                <button onclick="startEdit(${idx}, '${cat}')" style="padding: 5px 10px; width: auto; background: #FF9800; font-size: 12px; margin-left: 5px;">수정</button>
                <button onclick="deleteCategory('${cat}')" style="padding: 5px 10px; width: auto; background: #f44336; font-size: 12px; margin-left: 5px;">삭제</button>
            </div>
        `).map((html, idx) => html.replace('display: none; flex: 1; display: flex;', 'display: none; flex: 1;')).join('');
    }

    let editingIndex = null;

    function startEdit(idx, cat) {
        if (editingIndex !== null) cancelEdit(editingIndex);

        editingIndex = idx;
        document.getElementById(`cat-display-${idx}`).style.display = 'none';
        const editDiv = document.getElementById(`cat-edit-${idx}`);
        editDiv.style.display = 'flex';
        document.getElementById(`cat-input-${idx}`).focus();
    }

    function cancelEdit(idx) {
        document.getElementById(`cat-display-${idx}`).style.display = 'block';
        document.getElementById(`cat-edit-${idx}`).style.display = 'none';
        editingIndex = null;
    }

    async function saveEdit(idx) {
        const oldName = document.getElementById(`cat-display-${idx}`).textContent;
        const newName = document.getElementById(`cat-input-${idx}`).value.trim();

        if (!newName) {
            showToast('카테고리명을 입력해주세요', 'error');
            return;
        }

        if (newName === oldName) {
            cancelEdit(idx);
            return;
        }

        const form = new FormData();
        form.append('old_name', oldName);
        form.append('new_name', newName);

        const response = await fetch('/api/categories/rename', {method: 'POST', body: form});
        const data = await response.json();

        if (data.status === 'success') {
            renderCategoryList(data.categories);
            updateSelectOptions(data.categories);
            editingIndex = null;
        } else {
            showToast(data.error || '오류가 발생했습니다', 'error');
        }
    }

    function openCategoryModal() {
        const modal = document.getElementById('categoryModal');
        modal.style.display = 'flex';
        modal.style.justifyContent = 'center';
        modal.style.alignItems = 'center';
        loadCategories();
        setTimeout(() => {
            document.getElementById('newCategoryName')?.focus();
        }, 100);
    }

    function closeCategoryModal() {
        const modal = document.getElementById('categoryModal');
        modal.style.display = 'none';
    }

    // 모달 외부 클릭 시 닫기
    document.addEventListener('DOMContentLoaded', function() {
        const modal = document.getElementById('categoryModal');
        if (modal) {
            modal.addEventListener('click', function(e) {
                if (e.target === modal) {
                    closeCategoryModal();
                }
            });
        }
    });

    async function addNewCategory() {
        const name = document.getElementById('newCategoryName').value.trim();
        if (!name) {
            showToast('카테고리명을 입력해주세요', 'error');
            return;
        }

        const form = new FormData();
        form.append('name', name);

        const response = await fetch('/api/categories/add', {method: 'POST', body: form});
        const data = await response.json();

        if (data.status === 'success') {
            document.getElementById('newCategoryName').value = '';
            renderCategoryList(data.categories);
            updateSelectOptions(data.categories);
        } else {
            showToast(data.error || '오류가 발생했습니다', 'error');
        }
    }

    function updateSelectOptions(categories) {
        const select = document.getElementById('category');
        select.innerHTML = '';
        categories.forEach(cat => {
            const option = document.createElement('option');
            option.textContent = cat;
            select.appendChild(option);
        });
    }

    async function deleteCategory(name) {
        const form = new FormData();
        form.append('name', name);

        const response = await fetch('/api/categories/delete', {method: 'POST', body: form});
        const data = await response.json();

        if (data.status === 'success') {
            renderCategoryList(data.categories);
            updateSelectOptions(data.categories);
        } else {
            showToast(data.error || '오류가 발생했습니다', 'error');
        }
    }

    document.getElementById('date').valueAsDate = new Date();
    loadExpensesAndRender();
    loadCategories();
    </script>
</body>
</html>"""

# ── 카테고리 관리 API ──────────────────────────────
@app.get("/api/categories")
async def get_categories(session: Optional[str] = Cookie(None)):
    """사용자 카테고리 조회"""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    categories = user.get("categories", DEFAULT_CATEGORIES)
    return {"categories": categories}

@app.post("/api/categories/add")
async def add_category(name: str = Form(...), session: Optional[str] = Cookie(None)):
    """카테고리 추가"""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    categories = user.get("categories", DEFAULT_CATEGORIES)
    if name in categories:
        return JSONResponse({"error": "이미 존재하는 카테고리입니다"}, status_code=400)

    categories.append(name)
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"categories": categories}}
    )
    return {"status": "success", "categories": categories}

@app.post("/api/categories/delete")
async def delete_category(name: str = Form(...), session: Optional[str] = Cookie(None)):
    """카테고리 삭제"""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    categories = user.get("categories", DEFAULT_CATEGORIES)
    if name not in categories:
        return JSONResponse({"error": "존재하지 않는 카테고리입니다"}, status_code=400)

    if len(categories) <= 1:
        return JSONResponse({"error": "최소 1개의 카테고리는 필요합니다"}, status_code=400)

    categories.remove(name)
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"categories": categories}}
    )
    return {"status": "success", "categories": categories}

@app.post("/api/categories/rename")
async def rename_category(old_name: str = Form(...), new_name: str = Form(...), session: Optional[str] = Cookie(None)):
    """카테고리 수정"""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    categories = user.get("categories", DEFAULT_CATEGORIES)
    if old_name not in categories:
        return JSONResponse({"error": "존재하지 않는 카테고리입니다"}, status_code=400)

    if new_name in categories:
        return JSONResponse({"error": "이미 존재하는 카테고리입니다"}, status_code=400)

    idx = categories.index(old_name)
    categories[idx] = new_name
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"categories": categories}}
    )
    return {"status": "success", "categories": categories}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
