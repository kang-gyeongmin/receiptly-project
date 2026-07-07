from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import re
import calendar
import hashlib
import secrets
import bcrypt
import ssl
import certifi
import json as _json
import asyncio
from urllib.parse import quote
from urllib.request import urlopen, Request
from typing import Optional
import easyocr
import numpy as np
from PIL import Image, ImageEnhance, ImageOps
from io import BytesIO

# macOS 파이썬은 시스템 인증서를 안 써서 EasyOCR 모델 다운로드가 SSL 오류남 → certifi 사용
ssl._create_default_https_context = lambda *a, **k: ssl.create_default_context(cafile=certifi.where())

load_dotenv()

app = FastAPI()
client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME")]

# ODsay 대중교통 API 키 (지하철 요금 조회). lab.odsay.com 에서 무료 발급 → .env 에 ODSAY_API_KEY
ODSAY_API_KEY = os.getenv("ODSAY_API_KEY", "")

# 네이버 쇼핑 검색 API (위시리스트 제품 검색). developers.naver.com 에서 무료 발급
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

# PWA 정적 파일(아이콘 등)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── PWA (홈 화면 설치용 manifest + 서비스워커) ──────────
PWA_MANIFEST = {
    "name": "Zik 가계부",
    "short_name": "Zik",
    "description": "자연어·영수증으로 쉽게 쓰는 AI 가계부",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#f5f5f5",
    "theme_color": "#4A90D9",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}

@app.get("/manifest.json")
async def manifest():
    return JSONResponse(PWA_MANIFEST)

# 서비스워커: 네트워크 우선(오래된 HTML 캐시 방지), 실패 시 캐시로 폴백
SERVICE_WORKER_JS = """
const CACHE = 'receiptly-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request)
      .then(res => {
        // 아이콘 등 정적만 캐시(오프라인 대비)
        if (e.request.url.includes('/static/') || e.request.url.endsWith('/manifest.json')) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
"""

@app.get("/sw.js")
async def service_worker():
    return Response(SERVICE_WORKER_JS, media_type="application/javascript")

# OCR 리더 초기화 (배포 시 EASYOCR_MODEL_DIR에 미리 받은 모델 사용)
try:
    _easyocr_dir = os.getenv("EASYOCR_MODEL_DIR")
    if _easyocr_dir:
        reader = easyocr.Reader(['ko', 'en'], model_storage_directory=_easyocr_dir)
    else:
        reader = easyocr.Reader(['ko', 'en'])
except Exception as e:
    print(f"⚠️ OCR 초기화 실패: {e}")
    reader = None

# 기본 카테고리
DEFAULT_CATEGORIES = ["카페", "식사", "데이트", "고정지출"]
DEFAULT_ACCOUNTS = ["현금"]

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
    """bcrypt(솔트 포함) 해시. bcrypt는 72바이트 제한 → 초과분 잘림."""
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()

def verify_password(password: str, stored: str) -> bool:
    """bcrypt 해시면 bcrypt로, 레거시 sha256(64 hex)이면 sha256으로 검증."""
    if stored.startswith("$2"):  # bcrypt
        try:
            return bcrypt.checkpw(password.encode()[:72], stored.encode())
        except Exception:
            return False
    return hashlib.sha256(password.encode()).hexdigest() == stored  # 레거시

SESSION_DAYS = 30

def generate_token() -> str:
    return secrets.token_urlsafe(32)

async def create_session(user_id) -> str:
    token = generate_token()
    await db.sessions.insert_one({
        "user_id": user_id,
        "token": token,
        "expires_at": (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(),
    })
    return token

async def get_current_user(session: Optional[str] = Cookie(None)):
    if not session:
        return None
    session_doc = await db.sessions.find_one({"token": session})
    if not session_doc:
        return None
    # 만료 검증 (레거시 세션엔 expires_at 없을 수 있음 → 통과)
    exp = session_doc.get("expires_at")
    if exp and exp < datetime.now().isoformat():
        await db.sessions.delete_one({"token": session})
        return None
    return await db.users.find_one({"_id": session_doc["user_id"]})

# ── ⚠️ OCR 미사용 (Phase B 재연결 예정) ────────────────
# preprocess_image / extract_text_from_image 는 구현돼 있으나 아직 엔드포인트/UI 미연결.
# 영수증 캡처 기능 붙일 때 재사용 예정. (삭제 금지)
# ※ parse_natural_language 는 챗봇 자연어 저장에 실제 사용 중 (아래 별도 섹션).
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
        results = reader.readtext(np.array(image), detail=1)  # EasyOCR은 numpy 배열 필요
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

def extract_text_lines(image_bytes: bytes) -> str:
    """OCR 결과를 '시각적 줄'로 재구성(같은 y끼리 묶고 x로 정렬) → 줄바꿈 텍스트.
    통장/영수증처럼 표 형태에서 잔액 줄이 금액 줄과 분리됨."""
    if reader is None:
        return ""
    try:
        image = Image.open(BytesIO(image_bytes))
        image = preprocess_image(image)
        results = reader.readtext(np.array(image), detail=1)
        toks = []
        for box, text, conf in results:
            if conf < 0.3 or not text.strip():
                continue
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            toks.append((min(ys), min(xs), max(ys) - min(ys), text.strip()))
        if not toks:
            return ""
        toks.sort(key=lambda t: (t[0], t[1]))
        heights = sorted(t[2] for t in toks)
        thr = max(8, heights[len(heights) // 2] * 0.6)  # 줄 구분 임계값(글자높이 기반)
        lines = []
        cur = []
        prev_y = None
        for y, x, h, text in toks:
            if prev_y is not None and (y - prev_y) > thr:
                cur.sort(key=lambda p: p[0])
                lines.append(" ".join(t for _, t in cur))
                cur = []
            cur.append((x, text))
            prev_y = y
        if cur:
            cur.sort(key=lambda p: p[0])
            lines.append(" ".join(t for _, t in cur))
        return "\n".join(lines)
    except Exception as e:
        print(f"OCR(lines) 오류: {e}")
        return ""

# 영수증/결제내역 OCR 텍스트에서 지출 정보 추출 (best-effort, 확인 카드로 교정 전제)
RECEIPT_STOPWORDS = [
    "승인", "금액", "합계", "카드", "일시불", "할부", "매출", "가맹점", "결제", "부가세",
    "공급가", "잔액", "포인트", "적립", "거래", "번호", "판매", "영수증", "고객", "신용",
    "체크", "은행", "원", "총액", "받을", "주소", "전화", "대표",
]

def extract_expense_from_ocr(text: str) -> dict:
    """OCR 텍스트 → {date, store_name, amount}. 금액은 최댓값(=결제총액 추정)."""
    result = {"date": None, "store_name": None, "amount": None, "memo": ""}

    # ── 금액: '원' 붙은 금액 우선, 없으면 콤마 3자리 그룹 숫자. 그 중 최댓값 ──
    amounts = [int(m.group(1).replace(',', '')) for m in re.finditer(r'([\d,]{2,})\s*원', text)]
    if not amounts:
        amounts = [int(m.group(1).replace(',', '')) for m in re.finditer(r'(\d{1,3}(?:,\d{3})+)', text)]
    if amounts:
        result["amount"] = max(amounts)

    # ── 날짜: YYYY.MM.DD / YYYY-MM-DD / YYYY년 M월 D일 우선, 없으면 M월 D일 / M/D ──
    m = re.search(r'(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})', text)
    if m:
        result["date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    else:
        m = re.search(r'(\d{1,2})\s*월\s*(\d{1,2})\s*일', text) or re.search(r'(\d{1,2})[/-](\d{1,2})', text)
        if m:
            today = datetime.now()
            mo, d = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                yr = today.year - (1 if mo > today.month else 0)
                result["date"] = f"{yr}-{mo:02d}-{d:02d}"
    if not result["date"]:
        result["date"] = datetime.now().strftime("%Y-%m-%d")

    # ── 가게명: 한글 2자 이상 토큰 중 불용어/숫자 아닌 첫 후보 ──
    for tok in text.split():
        t = tok.strip(" .,·-()[]")
        if len(t) < 2 or re.search(r'\d', t):
            continue
        if not re.search(r'[가-힣]', t):
            continue
        if any(sw in t for sw in RECEIPT_STOPWORDS):
            continue
        result["store_name"] = t
        break

    return result

# 통장/카드 내역(여러 건) OCR → 거래 리스트. "날짜 설명 금액원" 반복 추출(잔액은 앞에 날짜가 없어 자동 제외)
_TXN_INCOME_KWS = ["급여", "월급", "이자", "입금", "혜택", "받기", "캐시백", "환급", "지원금", "정산", "이체입금"]

def extract_transactions_from_ocr(text: str) -> list:
    today = datetime.now()
    txns = []
    for m in re.finditer(r'(\d{1,2})[.\-/](\d{1,2})[ \t]+(.+?)[ \t]*(-?\d[\d,]*)[ \t]*원', text):
        mo, d = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        desc = re.sub(r'\s+', ' ', m.group(3)).strip(" .,-·|~−–—")
        raw = m.group(4).strip()
        amount = int(raw.replace(',', '').replace('-', '') or 0)
        if amount <= 0 or not desc:
            continue
        # 부호(-/~/−)=지출, 수입키워드=수입, 그 외 기본 지출. 확인 리스트에서 수정 가능
        tail = m.group(3).rstrip()[-1:]  # 설명 끝이 마이너스류면 지출
        if raw.startswith('-') or tail in "-~−–—":
            kind = "expense"
        elif any(k in desc for k in _TXN_INCOME_KWS):
            kind = "income"
        else:
            kind = "expense"
        yr = today.year - (1 if mo > today.month else 0)
        txns.append({
            "date": f"{yr}-{mo:02d}-{d:02d}",
            "store": desc[:40],
            "amount": amount,
            "kind": kind,
        })
    return txns

def keyword_category(categories: list, store: str) -> str:
    """DB 조회 없이 키워드로만 카테고리 추정 (여러 건 일괄용)."""
    low = (store or "").lower()
    for cat, kws in CATEGORY_KEYWORD_HINTS.items():
        if cat in categories and any(k in low for k in kws):
            return cat
    return categories[0] if categories else "기타"

# ── ⚠️ OCR 미사용 블록 끝 ──────────────────────────────

# ── 자연어 파싱 (챗봇 저장에 사용) ──────────────────────
def _extract_amount(s: str):
    """문자열 끝에서 금액을 추출. 만/천 단위, 콤마, '원' 지원.
    반환: (금액 or None, 금액 제거된 문자열)"""
    s2 = s.rstrip()
    patterns = [
        (r'(\d+)\s*만\s*(\d+)\s*천\s*원?$', lambda m: int(m.group(1)) * 10000 + int(m.group(2)) * 1000),
        (r'(\d+)\s*만\s*원?$',             lambda m: int(m.group(1)) * 10000),
        (r'(\d+)\s*천\s*원?$',             lambda m: int(m.group(1)) * 1000),
        (r'([\d,]+)\s*원$',                lambda m: int(m.group(1).replace(',', ''))),
        (r'(\d[\d,]*)$',                   lambda m: int(m.group(1).replace(',', ''))),
    ]
    for pat, conv in patterns:
        m = re.search(pat, s2)
        if m:
            return conv(m), s2[:m.start()]
    return None, s

def _extract_date(s: str):
    """'M월 D일' 또는 'M/D', 'M-D' 형식 추출. 반환: (YYYY-MM-DD or None, 날짜 제거된 문자열)"""
    m = re.search(r'(\d{1,2})\s*월\s*(\d{1,2})\s*일?', s)
    if not m:
        m = re.search(r'(\d{1,2})[/-](\d{1,2})', s)
    if not m:
        return None, s
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None, s
    today = datetime.now()
    year = today.year
    if month > today.month:  # 미래 달이면 작년으로 간주
        year -= 1
    date = f"{year}-{month:02d}-{day:02d}"
    return date, (s[:m.start()] + ' ' + s[m.end():])

def parse_natural_language(text: str) -> dict:
    result = {"date": None, "store_name": None, "amount": None, "memo": ""}
    work = text.strip()

    amount, work = _extract_amount(work)
    if amount is not None:
        result["amount"] = amount

    date, work = _extract_date(work)
    if date:
        result["date"] = date

    store = work.strip(" ·-\t")
    if store:
        result["store_name"] = store

    if not result["date"]:
        result["date"] = datetime.now().strftime("%Y-%m-%d")

    return result

# ── free-form 파서 (상대/다중 날짜 + 금액 위치 자유 + 카테고리 힌트) ──
INCOME_KEYWORDS = ["입금", "월급", "급여", "용돈", "수입", "보너스", "상여", "이체받", "받음", "환급"]

def _extract_all_dates(text: str):
    """상대/절대 날짜를 여러 개 추출. 반환: ([YYYY-MM-DD...], 날짜 제거된 text)"""
    today = datetime.now()
    found = []
    def add(dt):
        s = dt.strftime("%Y-%m-%d")
        if s not in found:
            found.append(s)

    # 상대 표현 (긴 단어 먼저 — 엊그제가 그제보다 앞)
    rel = [("그끄저께", 3), ("그끄제", 3), ("엊그제", 2), ("그저께", 2), ("그제", 2),
           ("어제", 1), ("오늘", 0), ("모레", -2), ("내일", -1)]
    for word, ago in rel:
        if word in text:
            add(today - timedelta(days=ago))
            text = text.replace(word, " ")

    # N일 전
    for m in re.finditer(r'(\d+)\s*일\s*전', text):
        add(today - timedelta(days=int(m.group(1))))
    text = re.sub(r'\d+\s*일\s*전', ' ', text)

    # 절대: M월 D일 (여러 개)
    for m in re.finditer(r'(\d{1,2})\s*월\s*(\d{1,2})\s*일?', text):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            add(datetime(today.year - (1 if mo > today.month else 0), mo, d))
    text = re.sub(r'(\d{1,2})\s*월\s*(\d{1,2})\s*일?', ' ', text)

    # 절대: M/D, M-D (여러 개)
    for m in re.finditer(r'(\d{1,2})[/-](\d{1,2})', text):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            add(datetime(today.year - (1 if mo > today.month else 0), mo, d))
    text = re.sub(r'(\d{1,2})[/-](\d{1,2})', ' ', text)

    return found, text

def _find_amount_anywhere(s: str):
    """문장 어디서든 금액 추출(끝 고정 아님). 반환: (금액 or None, 금액 제거된 text)"""
    patterns = [
        (r'(\d+)\s*만\s*(\d+)\s*천\s*원?', lambda m: int(m.group(1)) * 10000 + int(m.group(2)) * 1000),
        (r'(\d+)\s*만\s*원?',             lambda m: int(m.group(1)) * 10000),
        (r'(\d+)\s*천\s*원?',             lambda m: int(m.group(1)) * 1000),
        (r'([\d,]{2,})\s*원',             lambda m: int(m.group(1).replace(',', ''))),
        (r'([\d,]{2,})',                  lambda m: int(m.group(1).replace(',', ''))),
    ]
    for pat, conv in patterns:
        m = re.search(pat, s)
        if m:
            return conv(m), (s[:m.start()] + ' ' + s[m.end():])
    return None, s

_STORE_DROP = {
    # 조사/접속
    "랑", "이랑", "도", "과", "와", "그리고", "또", "좀", "에", "은", "는",
    "이", "가", "을", "를", "의",
    # 동사/명령
    "넣어줘", "넣어", "넣어줘요", "기록", "기록해줘", "기록해", "저장", "저장해줘", "해줘",
    "추가해줘", "추가", "썼어", "썼다", "샀어", "샀다", "냈어", "냈다", "썼", "샀",
}

def _clean_store(s: str) -> str:
    toks = []
    for t in re.split(r'\s+', s.strip()):
        if not t or t in _STORE_DROP:
            continue
        t = re.sub(r'(으로|로|에서|에|을|를|이|가|은|는|도|랑)$', '', t)
        if t and t not in _STORE_DROP:
            toks.append(t)
    return " ".join(toks).strip()

def parse_free_expense(text: str) -> dict:
    """자유 문장 → {dates:[...], amount, store, category, kind}. 여러 날짜면 dates 다수."""
    work = text.strip()
    dates, work = _extract_all_dates(work)
    amount, work = _find_amount_anywhere(work)

    # 카테고리 힌트
    category = None
    if re.search(r'교통|지하철|버스|택시', text):
        category = "교통"

    kind = "income" if any(k in text for k in INCOME_KEYWORDS) else "expense"
    store = _clean_store(work)

    if amount is not None and not dates:
        dates = [datetime.now().strftime("%Y-%m-%d")]

    return {"dates": dates, "amount": amount, "store": store, "category": category, "kind": kind}

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
        "accounts": DEFAULT_ACCOUNTS,
        "onboarded": False,  # 첫 접속 시 은행 등록 온보딩
        "created_at": datetime.now().isoformat()
    }
    result = await db.users.insert_one(user)

    # 자동 로그인
    token = await create_session(result.inserted_id)

    response = JSONResponse({"status": "success", "message": "회원가입 완료되었습니다"})
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400*SESSION_DAYS)
    return response

@app.post("/auth/login")
async def login(username: str = Form(...), password: str = Form(...)):
    user = await db.users.find_one({"username": username})
    if not user or not verify_password(password, user.get("password", "")):
        return JSONResponse({"error": "계정 정보가 일치하지 않습니다"}, status_code=400)

    # 레거시 sha256 계정이면 bcrypt로 자동 업그레이드
    if not user.get("password", "").startswith("$2"):
        await db.users.update_one({"_id": user["_id"]}, {"$set": {"password": hash_password(password)}})

    token = await create_session(user["_id"])

    response = JSONResponse({"status": "success"})
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400*SESSION_DAYS)
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
async def add_expense(date: str = Form(...), store: str = Form(...), amount: int = Form(...), category: str = Form(...), kind: str = Form("expense"), account: str = Form(""), session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    if kind not in ("expense", "income"):
        kind = "expense"

    doc = {
        "user_id": user["_id"],
        "date": date,
        "store_name": store,
        "amount": amount,
        "category": category,
        "kind": kind,  # "expense" | "income"
        "account": account.strip(),  # 계좌/카드 (예: 카카오뱅크, 현금)
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

@app.post("/api/expenses/update")
async def update_expense(
    id: str = Form(...),
    store: str = Form(...),
    amount: int = Form(...),
    category: str = Form(...),
    session: Optional[str] = Cookie(None),
):
    """지출 수정 (본인 소유만). 날짜는 그대로 두고 가게/금액/카테고리만 변경."""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    if amount <= 0:
        return JSONResponse({"error": "금액은 1원 이상이어야 합니다"}, status_code=400)

    try:
        oid = ObjectId(id)
    except InvalidId:
        return JSONResponse({"error": "잘못된 지출 id입니다"}, status_code=400)

    result = await db.expenses.update_one(
        {"_id": oid, "user_id": user["_id"]},
        {"$set": {"store_name": store, "amount": amount, "category": category}},
    )
    if result.matched_count == 0:
        return JSONResponse({"error": "지출 내역을 찾을 수 없습니다"}, status_code=404)
    return {"status": "success"}

@app.post("/api/expenses/delete")
async def delete_expense(id: str = Form(...), session: Optional[str] = Cookie(None)):
    """지출 삭제 (본인 소유만)."""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    try:
        oid = ObjectId(id)
    except InvalidId:
        return JSONResponse({"error": "잘못된 지출 id입니다"}, status_code=400)

    result = await db.expenses.delete_one({"_id": oid, "user_id": user["_id"]})
    if result.deleted_count == 0:
        return JSONResponse({"error": "지출 내역을 찾을 수 없습니다"}, status_code=404)
    return {"status": "success"}

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
async def get_analysis(period: str = "month", offset: int = 0, account: str = "", session: Optional[str] = Cookie(None)):
    """기간별(month/week/year) 집계 + 목록. offset=기간이동, account=계좌 필터(빈값=전체)."""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    if period not in ("month", "week", "year"):
        period = "month"

    start, end, label = get_period_range(period, offset)
    account = (account or "").strip()

    expenses = []
    category_totals = {}   # 지출 카테고리별
    account_totals = {}    # 계좌별 {acc: {income, expense}}
    expense_total = 0
    income_total = 0
    async for doc in db.expenses.find({
        "user_id": user["_id"],
        "date": {"$gte": start, "$lte": end}
    }).sort("date", -1):
        doc["_id"] = str(doc["_id"])
        doc.pop("user_id", None)  # ObjectId는 JSON 직렬화 불가 + 프론트 미사용
        acc = doc.get("account", "") or "(미지정)"
        amount = doc.get("amount", 0)
        is_income = doc.get("kind") == "income"

        # 계좌별 합계는 필터와 무관하게 전체로 집계 (계좌 비교용)
        if acc not in account_totals:
            account_totals[acc] = {"income": 0, "expense": 0}
        account_totals[acc]["income" if is_income else "expense"] += amount

        # 계좌 필터 적용
        if account and acc != account:
            continue

        expenses.append(doc)
        if is_income:
            income_total += amount
        else:  # 기본: 지출 (기존 데이터에 kind 없으면 지출로 간주)
            expense_total += amount
            cat = doc.get("category", "기타")
            category_totals[cat] = category_totals.get(cat, 0) + amount

    by_category = sorted(
        [{"category": c, "amount": a} for c, a in category_totals.items()],
        key=lambda x: x["amount"], reverse=True
    )
    by_account = sorted(
        [{"account": a, "income": v["income"], "expense": v["expense"], "net": v["income"] - v["expense"]}
         for a, v in account_totals.items()],
        key=lambda x: x["income"] + x["expense"], reverse=True
    )

    return {
        "period": period,
        "offset": offset,
        "label": label,
        "start": start,
        "end": end,
        "account": account,
        "total": expense_total,        # 하위호환: 기존 total = 지출 합계
        "expense_total": expense_total,
        "income_total": income_total,
        "net": income_total - expense_total,
        "by_category": by_category,
        "by_account": by_account,
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

# ── 지하철 요금 조회 (ODsay API) ───────────────────────
def _odsay_call(endpoint: str, params: dict) -> dict:
    """ODsay API 동기 호출. urlopen은 블로킹이므로 asyncio.to_thread로 감싸 사용."""
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    query += f"&apiKey={quote(ODSAY_API_KEY)}"
    url = f"https://api.odsay.com/v1/api/{endpoint}?{query}"
    with urlopen(url, timeout=8) as resp:
        return _json.loads(resp.read().decode("utf-8"))

def _clean_station(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r'(까지|부터|으로|로|행)$', '', name)
    return name.strip()

async def odsay_station_coord(name: str):
    """역 이름 → (x경도, y위도, 정식역명). 못 찾으면 '역' 붙여 재시도. 실패 시 None."""
    for q in (name, name + "역") if not name.endswith("역") else (name,):
        data = await asyncio.to_thread(_odsay_call, "searchStation", {"stationName": q})
        stations = (data.get("result") or {}).get("station") or []
        if stations:
            s = stations[0]
            return s.get("x"), s.get("y"), s.get("stationName", q)
    return None

async def odsay_subway_fare(start_name: str, end_name: str):
    """두 역 사이 대중교통 요금 조회. {payment, time, start, end} 또는 None."""
    s = await odsay_station_coord(_clean_station(start_name))
    e = await odsay_station_coord(_clean_station(end_name))
    if not s or not e:
        return None
    data = await asyncio.to_thread(
        _odsay_call, "searchPubTransPathT",
        {"SX": s[0], "SY": s[1], "EX": e[0], "EY": e[1]}
    )
    paths = (data.get("result") or {}).get("path") or []
    if not paths:
        return None
    info = paths[0].get("info") or {}
    if info.get("payment") is None:
        return None
    return {"payment": info["payment"], "time": info.get("totalTime"), "start": s[2], "end": e[2]}

# ── 챗봇 ──────────────────────────────────────────
@app.post("/chat")
async def chat(message: str = Form(...), session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    text = message.strip()

    # 지하철 요금 조회: "지하철 ... A(에서/부터) B ... 얼마" → ODsay로 요금 조회 후 저장 확인 카드
    if "지하철" in text:
        m = re.search(r'([가-힣A-Za-z0-9]{2,})\s*(?:에서|부터|~|->|→)\s*([가-힣A-Za-z0-9]{2,})', text)
        if not m:
            m = re.search(r'([가-힣A-Za-z0-9]{2,}역)\s+([가-힣A-Za-z0-9]{2,}역)', text)
        if m:
            if not ODSAY_API_KEY:
                return {"type": "message", "response": "지하철 요금 조회는 ODsay API 키가 필요해요. lab.odsay.com 에서 무료 발급 후 .env 에 ODSAY_API_KEY 로 넣어주세요."}
            start_name, end_name = m.group(1), m.group(2)
            try:
                fare = await odsay_subway_fare(start_name, end_name)
            except Exception as ex:
                return {"type": "message", "response": f"요금 조회 중 오류가 발생했어요: {ex}"}
            if not fare:
                return {"type": "message", "response": f"'{start_name}'→'{end_name}' 경로를 찾지 못했어요. 역 이름을 확인해주세요."}
            category = await classify_expense(user, "지하철 교통")
            time_str = f" (약 {fare['time']}분)" if fare.get("time") else ""
            return {
                "type": "confirm",
                "expense": {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "store": f"지하철 {fare['start']}→{fare['end']}",
                    "amount": fare["payment"],
                    "category": category,
                    "kind": "expense",
                },
                "categories": user.get("categories", DEFAULT_CATEGORIES),
                "response": f"{fare['start']}→{fare['end']} 지하철 요금은 {fare['payment']:,}원이에요{time_str}. 저장할까요?",
            }

    # 저장 의도 우선: 금액+가게(또는 카테고리)가 있고 "얼마/?"가 없으면 저장으로 판단
    # ("택시 12000원 썼어"처럼 '썼'이 있어도 금액+가게면 저장)
    fx = parse_free_expense(text)
    is_save = (fx["amount"] is not None and (fx["store"] or fx["category"])
               and "얼마" not in text and "?" not in text)
    if is_save:
        cats = user.get("categories", DEFAULT_CATEGORIES)
        category = fx["category"] if fx["category"] else await classify_expense(user, fx["store"])
        base = {"store": fx["store"], "amount": fx["amount"], "category": category, "kind": fx["kind"]}
        if len(fx["dates"]) <= 1:
            date = fx["dates"][0] if fx["dates"] else datetime.now().strftime("%Y-%m-%d")
            return {"type": "confirm", "expense": {"date": date, **base},
                    "categories": cats, "response": "이렇게 저장할까요?"}
        dates = sorted(fx["dates"])
        pretty = ", ".join(d[5:].replace("-", "/") for d in dates)
        return {"type": "confirm_multi", "dates": dates, "expense": base,
                "categories": cats, "response": f"{len(dates)}건({pretty})을 저장할까요?"}

    # 지출/금액 관련 질문 처리
    is_query = any(kw in text for kw in ["얼마", "지출", "썼", "쓴", "총", "합계"])
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

    return {"type": "message", "response": "예: \"이번달 얼마 썼어?\" / \"6/30 샐러디 9900원\" / \"월급 300만원\" / \"지하철 강남역에서 홍대입구역 얼마?\""}

@app.post("/chat/image")
async def chat_image(file: UploadFile = File(...), session: Optional[str] = Cookie(None)):
    """결제내역/영수증 사진 → OCR → 파싱 → 확인 카드 (저장은 확인 후)"""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)

    if reader is None:
        return {"type": "message", "response": "OCR이 준비되지 않았어요. 서버 로그를 확인해주세요."}

    image_bytes = await file.read()
    text = extract_text_lines(image_bytes)  # 줄 재구성 (표 형태 대응)
    if not text:
        return {"type": "message", "response": "사진에서 글자를 읽지 못했어요. 더 밝고 선명한 사진으로 다시 시도해주세요."}

    cats = user.get("categories", DEFAULT_CATEGORIES)

    # ① 통장/카드 여러 건 목록인지 먼저 시도
    txns = extract_transactions_from_ocr(text)
    if len(txns) >= 2:
        for t in txns:
            t["category"] = keyword_category(cats, t["store"])
        return {
            "type": "confirm_list",
            "items": txns,
            "categories": cats,
            "response": f"{len(txns)}건을 읽었어요. 확인하고 저장하세요. (수입/지출·금액 수정 가능)",
        }

    # ② 단건 영수증
    parsed = extract_expense_from_ocr(text)
    if parsed["amount"] is None:
        preview = text[:120]
        return {"type": "message", "response": f"금액을 찾지 못했어요. 인식된 내용: {preview}"}

    category = await classify_expense(user, parsed["store_name"] or "")
    return {
        "type": "confirm",
        "expense": {
            "date": parsed["date"],
            "store": parsed["store_name"] or "",
            "amount": parsed["amount"],
            "category": category,
            "kind": "expense",
        },
        "categories": cats,
        "response": "영수증에서 읽었어요. 확인 후 저장하세요.",
    }

# HTML 페이지들
LOGIN_PAGE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Receiptly - 로그인</title>
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#4A90D9">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="Zik">
    <link rel="apple-touch-icon" href="/static/icon-192.png">
    <script>
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function() { navigator.serviceWorker.register('/sw.js').catch(function(){}); });
    }
    </script>
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
        <h1 style="display:flex; align-items:center; justify-content:center; gap:8px;"><img src="/static/logo-cat.png" style="height:36px; border-radius:6px;" alt="" onerror="this.remove()" /> Zik</h1>

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
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#4A90D9">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="Zik">
    <link rel="apple-touch-icon" href="/static/icon-192.png">
    <script>
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function() { navigator.serviceWorker.register('/sw.js').catch(function(){}); });
    }
    </script>
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

        .chatbot { border: 1px solid #ddd; border-radius: 8px; display: flex; flex-direction: column; height: 500px; min-height: 0; }
        .chat-messages { flex: 1; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch; overscroll-behavior: contain; padding: 15px; font-size: 13px; }
        .chat-message { margin: 8px 0; padding: 10px; border-radius: 6px; word-wrap: break-word; overflow-wrap: anywhere; word-break: break-word; max-width: 100%; }
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

        /* ── 모바일: ☰ 메뉴 + 챗봇 FAB + 반응형 ── */
        .mobile-only { display: none; }
        .mobile-menu { display: none; position: absolute; top: 62px; right: 16px; background: white; border-radius: 10px; box-shadow: 0 6px 20px rgba(0,0,0,0.2); flex-direction: column; z-index: 1500; overflow: hidden; }
        .mobile-menu button { width: 170px; text-align: left; margin: 0; border-radius: 0; background: white; color: #333; padding: 14px 16px; border-bottom: 1px solid #eee; font-size: 15px; }
        .mobile-menu button:hover { background: #f5f5f5; }
        #chat-fab { display: none; position: fixed; right: 16px; bottom: 16px; width: 58px; height: 58px; border-radius: 50%; font-size: 26px; padding: 0; box-shadow: 0 4px 14px rgba(0,0,0,0.3); z-index: 1400; align-items: center; justify-content: center; }
        .chat-header-row { display: flex; justify-content: space-between; align-items: center; }
        /* 달력 탭에서만 입력/챗봇 표시. 분석·위시리스트에선 숨김 */
        .hide-input-ui .sidebar { display: none; }
        .hide-input-ui #chat-fab { display: none !important; }
        /* 로고(고양이 이미지 + Zik) */
        .header h1 { display: flex; align-items: center; gap: 8px; }
        .logo-img { height: 34px; width: auto; border-radius: 6px; }
        /* 챗봇 FAB 고양이 이미지 */
        #chat-fab { overflow: hidden; padding: 0; }
        #chat-fab .fab-img { width: 100%; height: 100%; object-fit: cover; }

        @media (max-width: 768px) {
            html, body { overflow-x: hidden; }
            .container { grid-template-columns: 1fr; padding: 10px; gap: 12px; max-width: 100%; }
            .main, .sidebar { padding: 16px; max-width: 100%; overflow-x: hidden; }
            .desktop-only { display: none !important; }
            .mobile-only { display: flex; }
            .tabs { display: none; }
            .header { margin-bottom: 16px; padding-bottom: 12px; }
            .header h1 { font-size: 24px; }
            .mobile-menu-btn { width: auto; padding: 6px 12px; font-size: 22px; margin: 0; }
            /* 입력창이 가로로 넘치지 않게 */
            input, select { min-width: 0; max-width: 100%; box-sizing: border-box; }
            input[type="date"] { -webkit-appearance: none; appearance: none; width: 100%; }
            .input-2col { display: flex; gap: 8px; }
            .input-2col input { flex: 1; min-width: 0; }
            /* 날짜 클릭 시 상세 내역 줄이 넘치면 접기 */
            .stat-item { flex-wrap: wrap; row-gap: 4px; }
            .selected-date-info, .stats { max-width: 100%; }
            img { max-width: 100%; }
            /* 달력이 좁은 폰에서 넘치지 않게 */
            .calendar { gap: 4px; }
            .calendar-label { font-size: 10px; padding: 5px 0; }
            .calendar-day { min-height: 54px; padding: 6px 2px; font-size: 12px; overflow: hidden; }
            .calendar-day .amount, .calendar-day div { font-size: 10px; }
            /* 위시리스트 항목이 가로로 넘치면 줄바꿈 */
            .wl-item { flex-wrap: wrap; row-gap: 6px; }
            .wl-item .category, .wl-item > div { min-width: 0; }
            #chat-fab { display: flex; }
            #chat-section { position: fixed; left: 8px; right: 8px; top: 66px; bottom: 84px; background: white; border-radius: 12px; box-shadow: 0 8px 30px rgba(0,0,0,0.3); z-index: 1450; flex-direction: column; padding: 12px; display: none; }
            #chat-section.open { display: flex; }
            #chat-section .chatbot { flex: 1; height: auto; min-height: 0; border: none; }
            #chat-section .chat-header-row { flex-shrink: 0; }
        }
    </style>
</head>
<body>
    <div id="toast-container"></div>
    <div class="container">
        <div class="main">
            <div class="header">
                <h1><img src="/static/logo-cat.png" class="logo-img" alt="" onerror="this.remove()" /> Zik</h1>
                <button onclick="logout()" class="desktop-only">로그아웃</button>
                <button class="mobile-only mobile-menu-btn" onclick="toggleMobileMenu()">☰</button>
            </div>
            <div id="mobile-menu" class="mobile-menu">
                <button onclick="mobileNav('calendar')">📅 달력</button>
                <button onclick="mobileNav('analysis')">📊 분석</button>
                <button onclick="mobileNav('wishlist')">🛍️ 위시리스트</button>
                <button onclick="logout()">🚪 로그아웃</button>
            </div>

            <div class="tabs">
                <button class="tab active" onclick="switchTab('calendar')">📅 달력</button>
                <button class="tab" onclick="switchTab('analysis')">📊 분석</button>
                <button class="tab" onclick="switchTab('wishlist')">🛍️ 위시리스트</button>
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

                <div class="period-nav" style="display: flex; align-items: center; justify-content: center; gap: 15px; margin-bottom: 12px;">
                    <button onclick="movePeriod(-1)" style="width: auto; padding: 8px 16px; margin: 0;">←</button>
                    <span id="period-label" style="font-weight: 600; font-size: 16px; min-width: 180px; text-align: center;"></span>
                    <button onclick="movePeriod(1)" style="width: auto; padding: 8px 16px; margin: 0;">→</button>
                </div>

                <div style="display:flex; align-items:center; gap:8px; margin-bottom:16px;">
                    <span style="font-size:13px; color:#666;">🏦 계좌</span>
                    <select id="account-filter" onchange="loadAnalysis()" style="flex:1; margin:0; padding:8px;"></select>
                </div>

                <div class="stats" id="account-summary" style="margin-bottom:16px;"></div>

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

            <!-- 위시리스트 탭 -->
            <div id="wishlist" class="tab-content">
                <div style="background:#f9f9f9; padding:15px; border-radius:8px; margin-bottom:20px;">
                    <h4 style="margin-bottom:10px;">🛍️ 위시리스트 추가</h4>
                    <div style="display:flex; gap:8px;">
                        <input type="text" id="wl-search" placeholder="제품명으로 검색 (예: 에어팟 프로)" style="margin:0; padding:10px; flex:1;" onkeypress="if(event.key=='Enter') searchWishlist()" />
                        <button onclick="searchWishlist()" style="margin:0; padding:10px; width:auto;">🔍 검색</button>
                    </div>
                    <div id="wl-results" style="margin-top:10px;"></div>

                    <div style="border-top:1px dashed #ddd; margin-top:12px; padding-top:12px;">
                        <input type="text" id="wl-name" placeholder="제품명" style="margin:0 0 8px; padding:10px;" />
                        <div style="display:flex; gap:8px;">
                            <input type="number" id="wl-price" placeholder="가격" style="margin:0; padding:10px; flex:1;" />
                            <input type="text" id="wl-image" placeholder="이미지 URL" style="margin:0; padding:10px; flex:2;" />
                        </div>
                        <input type="hidden" id="wl-url" />
                        <button onclick="addWishlist()" style="margin-top:8px; padding:10px;">추가</button>
                        <div style="font-size:11px; color:#999; margin-top:6px;">검색 결과를 클릭하면 제품명·가격·이미지가 아래에 채워져요. 직접 수정 후 추가할 수도 있어요.</div>
                    </div>
                </div>
                <div style="font-size:12px; color:#aaa; margin-bottom:8px;">↕️ 항목을 끌어서 순서를 바꿀 수 있어요</div>
                <div id="wishlist-list"></div>
            </div>
        </div>

        <!-- 사이드바 -->
        <div class="sidebar">
            <h3>✏️ 입력</h3>
            <div style="display:flex; gap:5px; margin:8px 0;">
                <button type="button" id="kind-expense" onclick="setKind('expense')" style="flex:1; margin:0; background:#4A90D9;">지출</button>
                <button type="button" id="kind-income" onclick="setKind('income')" style="flex:1; margin:0; background:#ccc;">수입</button>
            </div>
            <div class="input-2col">
                <input type="text" id="store" placeholder="가게명" />
                <input type="number" id="amount" placeholder="금액" />
            </div>
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
            <div style="display: flex; gap: 5px;">
                <select id="account" style="flex: 1;"></select>
                <button onclick="openAccountModal()" title="계좌 관리" style="width: 40px; padding: 10px; margin: 8px 0;">🏦</button>
            </div>
            <button onclick="addExpense()">저장</button>

            <hr class="desktop-only" />

            <div id="chat-section">
                <div class="chat-header-row">
                    <h3>💬 챗봇</h3>
                    <button class="mobile-only" onclick="toggleChat(false)" style="width:auto; margin:0; padding:4px 12px; background:#999;">✕</button>
                </div>
                <div class="chatbot">
                    <div class="chat-messages" id="messages">
                        <div class="chat-message bot">안녕하세요! 이렇게 해보세요:<br>• 저장: "6/30 샐러디 9900원", "월급 300만원"<br>• 조회: "이번달 얼마 썼어?"<br>• 지하철 요금: "지하철 강남역에서 홍대입구역 얼마?"</div>
                    </div>
                    <div class="chat-input">
                        <input type="file" id="chat-image" accept="image/*" style="display:none;" onchange="sendChatImage(this)" />
                        <button onclick="document.getElementById('chat-image').click()" title="영수증 사진" style="width:40px; padding:8px;">📷</button>
                        <input type="text" id="chat-input" placeholder="예: 6/30 샐러디 9900원" onkeypress="if(event.key=='Enter') sendChat()" />
                        <button onclick="sendChat()">→</button>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <button id="chat-fab" onclick="toggleChat(true)"><img src="/static/chat-cat.png" class="fab-img" alt="챗봇" onerror="this.parentNode.textContent='🤖'" /></button>

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

    <!-- 계좌 관리 모달 -->
    <div id="accountModal" style="display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center;">
        <div style="background: white; border-radius: 12px; padding: 30px; width: 90%; max-width: 400px; box-shadow: 0 10px 40px rgba(0,0,0,0.2);">
            <h2>🏦 계좌/카드 관리</h2>
            <div style="margin: 20px 0;">
                <h4 style="margin-bottom: 10px;">내 계좌/카드</h4>
                <div id="accountList" style="max-height: 250px; overflow-y: auto;"></div>
            </div>
            <h4 style="margin-top: 20px; margin-bottom: 10px;">새 계좌/카드 추가</h4>
            <div style="display: flex; gap: 5px;">
                <input type="text" id="newAccountName" placeholder="예: 카카오뱅크, 신한체크" style="flex: 1;" />
                <button onclick="addNewAccount()" style="width: 60px;">추가</button>
            </div>
            <button onclick="closeAccountModal()" style="margin-top: 20px; background: #999;">닫기</button>
        </div>
    </div>

    <!-- 첫 접속 온보딩: 은행 등록 -->
    <div id="onboardModal" style="display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.55); z-index: 1100; align-items: center; justify-content: center;">
        <div style="background: white; border-radius: 14px; padding: 26px; width: 92%; max-width: 420px; max-height: 88vh; overflow-y: auto; box-shadow: 0 10px 40px rgba(0,0,0,0.25);">
            <h2 style="margin-bottom: 6px;">🏦 환영해요!</h2>
            <p style="color: #666; font-size: 14px; margin-bottom: 16px; line-height: 1.5;">쓰는 은행/카드를 등록하면 내역을 계좌별로 관리할 수 있어요.<br>(나중에 🏦 버튼에서 바꿀 수 있어요)</p>
            <div style="font-size: 13px; font-weight: 600; margin-bottom: 6px;">자주 쓰는 곳 (눌러서 추가)</div>
            <div id="bankChips" style="display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px;"></div>
            <div style="display: flex; gap: 5px; margin-bottom: 16px;">
                <input type="text" id="onboardInput" placeholder="직접 입력 (예: 케이뱅크)" style="flex: 1;" onkeypress="if(event.key=='Enter') onboardAddCustom()" />
                <button onclick="onboardAddCustom()" style="width: 60px;">추가</button>
            </div>
            <div style="font-size: 13px; font-weight: 600; margin-bottom: 6px;">내 계좌/카드</div>
            <div id="onboardList" style="max-height: 180px; overflow-y: auto; margin-bottom: 18px;"></div>
            <button onclick="onboardComplete()" style="padding: 13px; font-size: 15px;">시작하기</button>
        </div>
    </div>

    <script>
    let currentMonth = new Date();
    let selectedDate = null;
    let monthExpenses = [];
    let dateExpenses = [];          // 선택된 날짜의 지출 목록
    let editingExpenseIndex = null; // 인라인 수정 중인 항목 인덱스
    let userCategories = [];        // 사용자 카테고리 (수정/추가 드롭다운용)
    let userAccounts = [];          // 사용자 계좌/카드

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
        if (event && event.target && event.target.classList && event.target.classList.contains('tab')) {
            event.target.classList.add('active');
        }

        // 입력/챗봇은 '달력' 탭에서만 표시. 분석·위시리스트에선 숨김
        const isCalendar = (tab === 'calendar');
        document.body.classList.toggle('hide-input-ui', !isCalendar);
        const container = document.querySelector('.container');
        if (container) container.style.gridTemplateColumns = isCalendar ? '' : '1fr';
        const cs = document.getElementById('chat-section');
        if (cs && cs.classList.contains('open')) toggleChat(false);  // 탭 이동 시 챗봇 닫고 배경 잠금 해제
        const mm = document.getElementById('mobile-menu');
        if (mm) mm.style.display = 'none';              // 메뉴 닫기

        if (tab === 'calendar') renderCalendar();
        else if (tab === 'wishlist') loadWishlist();
        else loadAnalysis();
    }

    function toggleMobileMenu() {
        const m = document.getElementById('mobile-menu');
        if (m) m.style.display = (m.style.display === 'flex') ? 'none' : 'flex';
    }
    function mobileNav(tab) {
        switchTab(tab);
    }
    let chatScrollY = 0;
    function toggleChat(open) {
        const s = document.getElementById('chat-section');
        if (!s) return;
        if (open === undefined) open = !s.classList.contains('open');
        s.classList.toggle('open', open);
        const body = document.body;
        if (open) {
            // 뒤 배경 스크롤 완전 잠금 (iOS 대응: position fixed)
            chatScrollY = window.scrollY || window.pageYOffset || 0;
            body.style.position = 'fixed';
            body.style.top = '-' + chatScrollY + 'px';
            body.style.left = '0';
            body.style.right = '0';
            body.style.width = '100%';
            const m = document.getElementById('messages');
            if (m) m.scrollTop = m.scrollHeight;  // 최신 대화로
        } else {
            body.style.position = '';
            body.style.top = '';
            body.style.left = '';
            body.style.right = '';
            body.style.width = '';
            window.scrollTo(0, chatScrollY);
        }
    }

    // ── 위시리스트 ──
    let wishlistItems = [];
    let wlDragIndex = null;
    let wlSearchResults = [];

    async function searchWishlist() {
        const q = document.getElementById('wl-search').value.trim();
        if (!q) return;
        const box = document.getElementById('wl-results');
        box.innerHTML = '<div style="color:#999; font-size:13px;">검색 중...</div>';
        try {
            const res = await fetch('/api/wishlist/search?q=' + encodeURIComponent(q));
            const data = await res.json();
            if (!res.ok) { box.innerHTML = '<div style="color:#f44336; font-size:12px;">' + (data.error || '검색 실패') + '</div>'; return; }
            wlSearchResults = data.items || [];
            if (wlSearchResults.length === 0) { box.innerHTML = '<div style="color:#999; font-size:13px;">검색 결과가 없어요.</div>'; return; }
            box.innerHTML = wlSearchResults.map((r, i) =>
                '<div onclick="selectWlResult(' + i + ')" style="display:flex; align-items:center; gap:8px; padding:6px; border:1px solid #eee; border-radius:6px; margin-bottom:5px; cursor:pointer; background:white;">' +
                    (r.image ? '<img src="' + escapeHtml(r.image) + '" style="width:40px; height:40px; object-fit:cover; border-radius:4px; flex-shrink:0;" onerror="this.remove()" />' : '') +
                    '<div style="flex:1; min-width:0;">' +
                        '<div style="font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">' + escapeHtml(r.title) + '</div>' +
                        '<div style="font-size:12px; color:#4A90D9; font-weight:600;">' + Number(r.price).toLocaleString() + '원 <span style="color:#aaa; font-weight:400;">' + escapeHtml(r.mall) + '</span></div>' +
                    '</div>' +
                '</div>'
            ).join('');
        } catch (e) { box.innerHTML = '<div style="color:#f44336; font-size:12px;">검색 중 오류가 발생했어요</div>'; }
    }

    function selectWlResult(i) {
        const r = wlSearchResults[i];
        if (!r) return;
        document.getElementById('wl-name').value = r.title;
        document.getElementById('wl-price').value = r.price;
        document.getElementById('wl-image').value = r.image || '';
        // 네이버 상품 링크는 외부에서 열면 차단되므로 저장 안 함 → 백엔드가 쿠팡 검색 URL 생성
        document.getElementById('wl-url').value = '';
        showToast('아래에 채웠어요. 확인 후 추가하세요', 'success');
    }

    async function loadWishlist() {
        try {
            const res = await fetch('/api/wishlist');
            const data = await res.json();
            wishlistItems = data.items || [];
            renderWishlist();
        } catch (e) {
            console.error('위시리스트 로드 오류:', e);
        }
    }

    function renderWishlist() {
        const box = document.getElementById('wishlist-list');
        if (wishlistItems.length === 0) {
            box.innerHTML = '<div style="text-align:center; color:#999; padding:30px;">위시리스트가 비어있어요. 위에서 추가해보세요!</div>';
            return;
        }
        box.innerHTML = wishlistItems.map((it, i) => {
            const img = it.image_url
                ? '<img src="' + escapeHtml(it.image_url) + '" style="width:56px; height:56px; object-fit:cover; border-radius:6px; flex-shrink:0;" onerror="this.remove()" />'
                : '<div style="width:56px; height:56px; background:#eee; border-radius:6px; display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0;">🛍️</div>';
            // 네이버 상품 링크는 외부 접속이 차단되므로, 쿠팡 검색 링크로 대체(기존 항목 포함)
            let u = it.url || '';
            if (!u || u.indexOf('naver') !== -1) u = 'https://www.coupang.com/np/search?q=' + encodeURIComponent(it.name);
            const label = u.indexOf('coupang') !== -1 ? '쿠팡' : '링크';
            return '<div class="wl-item" data-i="' + i + '" ' +
                'ondragover="event.preventDefault()" ondrop="wlDrop(' + i + ')" ' +
                'style="display:flex; align-items:center; gap:12px; padding:10px; background:white; border:1px solid #eee; border-radius:8px; margin-bottom:8px;">' +
                '<span draggable="true" ondragstart="wlDragStart(' + i + ')" title="드래그해서 순서 변경" style="color:#bbb; cursor:grab; padding:0 4px; font-size:18px;">⠿</span>' +
                img +
                '<div style="flex:1; min-width:0;">' +
                    '<div style="font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">' + escapeHtml(it.name) + '</div>' +
                    '<div style="color:#4A90D9; font-weight:600;">' + Number(it.price).toLocaleString() + '원</div>' +
                '</div>' +
                '<a href="' + escapeHtml(u) + '" target="_blank" rel="noopener" style="text-decoration:none; background:#03C75A; color:white; padding:6px 10px; border-radius:6px; font-size:12px; white-space:nowrap;">' + label + ' 🔗</a>' +
                '<button onclick="deleteWishlist(' + i + ')" style="width:auto; margin:0; padding:6px 10px; font-size:12px; background:#f44336;">삭제</button>' +
            '</div>';
        }).join('');
    }

    async function addWishlist() {
        const name = document.getElementById('wl-name').value.trim();
        const price = document.getElementById('wl-price').value;
        const image = document.getElementById('wl-image').value.trim();
        const url = document.getElementById('wl-url').value.trim();
        if (!name || price === '') { showToast('제품명과 가격을 입력해주세요', 'error'); return; }

        const form = new FormData();
        form.append('name', name);
        form.append('price', parseInt(price));
        form.append('image_url', image);
        form.append('url', url);
        try {
            const res = await fetch('/api/wishlist/add', {method: 'POST', body: form});
            const data = await res.json();
            if (data.status === 'success') {
                document.getElementById('wl-name').value = '';
                document.getElementById('wl-price').value = '';
                document.getElementById('wl-image').value = '';
                document.getElementById('wl-url').value = '';
                document.getElementById('wl-search').value = '';
                document.getElementById('wl-results').innerHTML = '';
                showToast('추가 완료', 'success');
                loadWishlist();
            } else {
                showToast(data.error || '추가 실패', 'error');
            }
        } catch (e) { showToast('추가 중 오류가 발생했습니다', 'error'); }
    }

    async function deleteWishlist(i) {
        const it = wishlistItems[i];
        if (!it) return;
        const form = new FormData();
        form.append('id', it._id);
        try {
            const res = await fetch('/api/wishlist/delete', {method: 'POST', body: form});
            const data = await res.json();
            if (data.status === 'success') { showToast('삭제 완료', 'success'); loadWishlist(); }
            else showToast(data.error || '삭제 실패', 'error');
        } catch (e) { showToast('삭제 중 오류가 발생했습니다', 'error'); }
    }

    function wlDragStart(i) { wlDragIndex = i; }
    async function wlDrop(i) {
        if (wlDragIndex === null || wlDragIndex === i) return;
        const moved = wishlistItems.splice(wlDragIndex, 1)[0];
        wishlistItems.splice(i, 0, moved);
        wlDragIndex = null;
        renderWishlist();  // 즉시 반영
        // 서버에 새 순서 저장
        const form = new FormData();
        form.append('ids', JSON.stringify(wishlistItems.map(it => it._id)));
        try { await fetch('/api/wishlist/reorder', {method: 'POST', body: form}); }
        catch (e) { showToast('순서 저장 실패', 'error'); }
    }

    function prevMonth() {
        currentMonth.setMonth(currentMonth.getMonth() - 1);
        loadExpensesAndRender();
    }

    function nextMonth() {
        currentMonth.setMonth(currentMonth.getMonth() + 1);
        loadExpensesAndRender();
    }

    // 달력 칸용 금액 축약: 1만 이상은 '만' 단위(예: 166,820 → 16.7만), 그 이하는 그대로
    function fmtCalAmount(n) {
        if (n >= 10000) {
            const m = n / 10000;
            return (m >= 100 ? Math.round(m) : Math.round(m * 10) / 10) + '만';
        }
        return n.toLocaleString();
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
            let incomeTotal = 0;
            let expenseTotal = 0;
            dayExpenses.forEach(e => {
                if (e.kind === 'income') {
                    incomeTotal += e.amount;
                } else {
                    expenseTotal += e.amount;
                    if (!categoryTotals[e.category]) categoryTotals[e.category] = 0;
                    categoryTotals[e.category] += e.amount;
                }
            });

            // HTML 구성
            let html = '<div class="date">' + day + '</div>';
            if (dayExpenses.length > 0) {
                const isMobile = window.matchMedia('(max-width: 768px)').matches;
                html += '<div style="font-size: 11px; margin-top: 4px;">';
                if (isMobile) {
                    // 모바일: 카테고리 없이 -지출/+수입 총액만 (한 줄씩, 큰 금액은 만 단위 축약)
                    if (expenseTotal > 0) html += '<div style="color: #e74c3c; margin-bottom: 2px; white-space: nowrap; overflow: hidden;">-' + fmtCalAmount(expenseTotal) + '</div>';
                    if (incomeTotal > 0) html += '<div style="color: #2e7d32; margin-bottom: 2px; white-space: nowrap; overflow: hidden;">+' + fmtCalAmount(incomeTotal) + '</div>';
                } else {
                    // 데스크톱: 수입 + 카테고리별 지출
                    if (incomeTotal > 0) html += '<div style="color: #2e7d32; margin-bottom: 2px;">+' + incomeTotal.toLocaleString() + '원</div>';
                    for (const [cat, total] of Object.entries(categoryTotals)) {
                        html += '<div style="color: #666; margin-bottom: 2px;">' + escapeHtml(cat) + ' ' + total.toLocaleString() + '원</div>';
                    }
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
            const response = await fetch('/api/expenses/by-date?date=' + dateStr);
            if (!response.ok) throw new Error('API 응답 실패: ' + response.status);
            const data = await response.json();
            dateExpenses = data.expenses || [];
            editingExpenseIndex = null;
            renderDateExpenses(dateStr);
            document.getElementById('selected-date-section').style.display = 'block';
        } catch (e) {
            console.error('지출 조회 오류:', e);
            showToast('지출 내역을 불러올 수 없습니다', 'error');
        }
    }

    function catOptions(selected) {
        let cats = userCategories.slice();
        if (selected && cats.indexOf(selected) === -1) cats = [selected].concat(cats);  // 목록에 없는 값(예: 교통)도 선택지로
        return cats.map(c =>
            '<option value="' + escapeHtml(c) + '"' + (c === selected ? ' selected' : '') + '>' + escapeHtml(c) + '</option>'
        ).join('');
    }

    // 선택된 날짜의 지출 목록 렌더 (수정/삭제 버튼 + 추가 폼)
    function renderDateExpenses(dateStr) {
        const statsDiv = document.getElementById('selected-date-stats');
        let html = '<h4>📅 ' + dateStr + ' 지출 내역 (' + dateExpenses.length + '건)</h4>';

        if (dateExpenses.length === 0) {
            html += '<div class="stat-item"><span>이 날짜에 지출이 없습니다</span></div>';
        } else {
            dateExpenses.forEach((e, i) => {
                if (i === editingExpenseIndex) {
                    html +=
                        '<div class="stat-item" style="flex-direction:column; align-items:stretch; gap:5px;">' +
                            '<input id="edit-store" value="' + escapeHtml(e.store_name) + '" style="margin:0; padding:6px; font-size:13px;" />' +
                            '<div style="display:flex; gap:5px;">' +
                                '<input id="edit-amount" type="number" value="' + e.amount + '" style="margin:0; padding:6px; font-size:13px; flex:1;" />' +
                                '<select id="edit-cat" style="margin:0; padding:6px; font-size:13px; flex:1;">' + catOptions(e.category) + '</select>' +
                            '</div>' +
                            '<div style="display:flex; gap:5px;">' +
                                '<button onclick="saveEditExpense(' + i + ')" style="flex:1; margin:0; padding:6px; font-size:12px;">저장</button>' +
                                '<button onclick="cancelEditExpense()" style="flex:1; margin:0; padding:6px; font-size:12px; background:#999;">취소</button>' +
                            '</div>' +
                        '</div>';
                } else {
                    const isIncome = e.kind === 'income';
                    const amtColor = isIncome ? '#2e7d32' : '#4A90D9';
                    const amtText = (isIncome ? '+' : '') + e.amount.toLocaleString() + '원';
                    const tag = isIncome ? ' <span style="font-size:11px; color:#2e7d32;">[수입]</span>' : '';
                    html +=
                        '<div class="stat-item">' +
                            '<span class="category">' + escapeHtml(e.store_name) + tag + ' <span style="font-size:12px; color:#999;">(' + escapeHtml(e.category) + ')</span></span>' +
                            '<span style="display:flex; align-items:center; gap:6px;">' +
                                '<span class="amount" style="color:' + amtColor + ';">' + amtText + '</span>' +
                                '<button onclick="startEditExpense(' + i + ')" style="width:auto; margin:0; padding:3px 8px; font-size:11px; background:#FF9800;">수정</button>' +
                                '<button onclick="deleteExpense(' + i + ')" style="width:auto; margin:0; padding:3px 8px; font-size:11px; background:#f44336;">삭제</button>' +
                            '</span>' +
                        '</div>';
                }
            });
            const expTotal = dateExpenses.filter(e => e.kind !== 'income').reduce((s, e) => s + e.amount, 0);
            const incTotal = dateExpenses.filter(e => e.kind === 'income').reduce((s, e) => s + e.amount, 0);
            html += '<div class="stat-item" style="border-top:2px solid #ddd; padding-top:10px; margin-top:10px; font-weight:600;"><span>지출 합계</span><span style="color:#4A90D9;">' + expTotal.toLocaleString() + '원</span></div>';
            if (incTotal > 0) {
                html += '<div class="stat-item" style="font-weight:600;"><span>수입 합계</span><span style="color:#2e7d32;">+' + incTotal.toLocaleString() + '원</span></div>';
            }
        }

        statsDiv.innerHTML = html;
    }

    function startEditExpense(i) {
        editingExpenseIndex = i;
        renderDateExpenses(selectedDate);
    }

    function cancelEditExpense() {
        editingExpenseIndex = null;
        renderDateExpenses(selectedDate);
    }

    async function saveEditExpense(i) {
        const e = dateExpenses[i];
        const store = document.getElementById('edit-store').value.trim();
        const amount = document.getElementById('edit-amount').value;
        const category = document.getElementById('edit-cat').value;
        if (!store || !amount) { showToast('가게명과 금액을 입력해주세요', 'error'); return; }

        const form = new FormData();
        form.append('id', e._id);
        form.append('store', store);
        form.append('amount', parseInt(amount));
        form.append('category', category);
        try {
            const res = await fetch('/api/expenses/update', {method: 'POST', body: form});
            const result = await res.json();
            if (result.status === 'success') {
                editingExpenseIndex = null;
                showToast('수정 완료', 'success');
                await showDateExpenses(selectedDate);
                loadExpensesAndRender();
            } else {
                showToast(result.error || '수정 실패', 'error');
            }
        } catch (err) { showToast('수정 중 오류가 발생했습니다', 'error'); }
    }

    async function deleteExpense(i) {
        const e = dateExpenses[i];
        const form = new FormData();
        form.append('id', e._id);
        try {
            const res = await fetch('/api/expenses/delete', {method: 'POST', body: form});
            const result = await res.json();
            if (result.status === 'success') {
                showToast('삭제 완료', 'success');
                await showDateExpenses(selectedDate);
                loadExpensesAndRender();
            } else {
                showToast(result.error || '삭제 실패', 'error');
            }
        } catch (err) { showToast('삭제 중 오류가 발생했습니다', 'error'); }
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

        const acctFilter = document.getElementById('account-filter');
        const selectedAcct = acctFilter ? acctFilter.value : '';

        try {
            const response = await fetch('/api/analysis?period=' + currentPeriod + '&offset=' + periodOffset + '&account=' + encodeURIComponent(selectedAcct));
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

            // 계좌 필터 옵션 채우기 (선택 유지)
            if (acctFilter) {
                const cur = acctFilter.value;
                let opts = '<option value="">전체 계좌</option>';
                (userAccounts || []).forEach(a => { opts += '<option value="' + escapeHtml(a) + '">' + escapeHtml(a) + '</option>'; });
                acctFilter.innerHTML = opts;
                acctFilter.value = cur;
            }

            // 계좌별 수입/지출/순액 요약
            const byAccount = data.by_account || [];
            const accDiv = document.getElementById('account-summary');
            if (accDiv) {
                if (byAccount.length <= 1 && !selectedAcct) {
                    accDiv.innerHTML = '';  // 계좌 하나뿐이면 생략
                } else {
                    let ah = '<h4>계좌별</h4>';
                    byAccount.forEach(a => {
                        const net = a.net;
                        ah += '<div class="stat-item"><span class="category">' + escapeHtml(a.account) + '</span>' +
                              '<span style="font-size:12px;"><span style="color:#2e7d32;">+' + a.income.toLocaleString() + '</span> / <span style="color:#4A90D9;">-' + a.expense.toLocaleString() + '</span> = <b style="color:' + (net >= 0 ? '#2e7d32' : '#f44336') + ';">' + (net >= 0 ? '+' : '') + net.toLocaleString() + '</b></span></div>';
                    });
                    accDiv.innerHTML = ah;
                }
            }

            // 수입/지출/순수지 요약 + 카테고리별 지출
            const incomeTotal = data.income_total || 0;
            const expenseTotal = data.expense_total || 0;
            const net = data.net || 0;
            const netColor = net >= 0 ? '#2e7d32' : '#f44336';

            let statsHtml = '<h4>' + periodLabel + '</h4>' +
                '<div class="stat-item"><span>수입</span><span style="color:#2e7d32; font-weight:600;">+' + incomeTotal.toLocaleString() + '원</span></div>' +
                '<div class="stat-item"><span>지출</span><span style="color:#4A90D9; font-weight:600;">-' + expenseTotal.toLocaleString() + '원</span></div>' +
                '<div class="stat-item" style="border-top:2px solid #ddd; padding-top:8px; margin-top:4px;"><span style="font-weight:600;">순수지</span><span style="color:' + netColor + '; font-weight:700;">' + (net >= 0 ? '+' : '') + net.toLocaleString() + '원</span></div>' +
                '<div style="font-size:13px; font-weight:600; margin:14px 0 4px;">카테고리별 지출</div>';
            if (byCategory.length === 0) {
                statsHtml += '<div class="stat-item"><span>지출 내역이 없습니다</span></div>';
            } else {
                statsHtml += byCategory.map(c =>
                    '<div class="stat-item"><span class="category">' + escapeHtml(c.category) + '</span><span class="amount">' + c.amount.toLocaleString() + '원</span></div>'
                ).join('');
            }
            statsDiv.innerHTML = statsHtml;

            // 내역 테이블 (수입은 초록 +)
            if (expenses.length === 0) {
                tableBody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #999;">내역이 없습니다</td></tr>';
            } else {
                tableBody.innerHTML = expenses.map(e => {
                    const inc = e.kind === 'income';
                    const amtCell = '<td style="text-align:right; color:' + (inc ? '#2e7d32' : '#333') + ';">' + (inc ? '+' : '') + e.amount.toLocaleString() + '원</td>';
                    const catCell = '<td>' + (inc ? '수입' : escapeHtml(e.category)) + '</td>';
                    return '<tr><td>' + escapeHtml(e.date) + '</td><td>' + escapeHtml(e.store_name) + '</td>' + amtCell + catCell + '</tr>';
                }).join('');
            }
        } catch (e) {
            console.error('분석 데이터 로드 오류:', e);
            statsDiv.innerHTML = '<h4>카테고리별 지출</h4><div class="stat-item"><span>데이터를 불러올 수 없습니다</span></div>';
            tableBody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #999;">데이터를 불러올 수 없습니다</td></tr>';
        }
    }

    let inputKind = 'expense';  // 사이드바 입력 종류
    function setKind(k) {
        inputKind = k;
        document.getElementById('kind-expense').style.background = (k === 'expense') ? '#4A90D9' : '#ccc';
        document.getElementById('kind-income').style.background = (k === 'income') ? '#2e7d32' : '#ccc';
        document.getElementById('store').placeholder = (k === 'income') ? '수입원 (예: 월급)' : '가게명';
    }

    async function addExpense() {
        const store = document.getElementById('store').value;
        const amount = document.getElementById('amount').value;
        const date = document.getElementById('date').value;
        const category = document.getElementById('category').value;
        const account = document.getElementById('account').value;

        if (!store || !amount || !date) {
            showToast('모든 항목을 입력해주세요', 'error');
            return;
        }

        const form = new FormData();
        form.append('date', date);
        form.append('store', store);
        form.append('amount', parseInt(amount));
        form.append('category', category);
        form.append('kind', inputKind);
        form.append('account', account);

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
    let lastPending = null;  // {type:'single'|'multi', n} — 답장으로 저장/취소 처리용

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    function isAffirmative(s) {
        if (/(취소|아니|아냐|ㄴㄴ|안 *해|하지 *마)/.test(s)) return false;
        return /저장|기록|응|네|넵|예|그래|좋아|어어|ㅇㅇ|ㅇㅋ|오케이|콜|ok|okay|yes/i.test(s) || /^(ㅇ+|응+|어+)$/.test(s);
    }
    function isNegative(s) {
        return /(취소|아니|아냐|ㄴㄴ|안 *해|하지 *마|안돼|그만)/.test(s);
    }

    async function sendChat() {
        const inputEl = document.getElementById('chat-input');
        const input = inputEl.value.trim();
        if (!input) return;

        const messages = document.getElementById('messages');
        messages.innerHTML += '<div class="chat-message user">' + escapeHtml(input) + '</div>';
        inputEl.value = '';
        messages.scrollTop = messages.scrollHeight;

        // 확인 카드가 떠 있으면 "응/저장/네" → 저장, "아니/취소" → 취소
        if (lastPending) {
            const p = lastPending;
            if (isNegative(input)) {
                lastPending = null;
                if (p.type === 'multi') cancelMulti(p.n); else cancelSave(p.n);
                return;
            }
            if (isAffirmative(input)) {
                lastPending = null;
                if (p.type === 'multi') await confirmSaveMulti(p.n); else await confirmSave(p.n);
                return;
            }
        }
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
            } else if (data.type === 'confirm_multi' && data.expense) {
                renderConfirmMultiCard(data, data.categories || []);
            } else {
                messages.innerHTML += '<div class="chat-message bot">' + (data.response || '') + '</div>';
            }
        } catch (e) {
            messages.innerHTML += '<div class="chat-message bot">오류가 발생했어요. 다시 시도해주세요.</div>';
        }
        messages.scrollTop = messages.scrollHeight;
    }

    // 영수증 사진 업로드 → OCR → 확인 카드
    async function sendChatImage(inputEl) {
        const fileEl = inputEl || document.getElementById('chat-image');
        const file = fileEl.files && fileEl.files[0];
        if (!file) return;

        const messages = document.getElementById('messages');
        messages.innerHTML += '<div class="chat-message user">📷 ' + escapeHtml(file.name) + '</div>';
        messages.innerHTML += '<div class="chat-message bot" id="ocr-loading">영수증을 읽는 중...</div>';
        messages.scrollTop = messages.scrollHeight;
        fileEl.value = '';  // 같은 파일 다시 선택 가능하도록

        try {
            const form = new FormData();
            form.append('file', file);
            const res = await fetch('/chat/image', {method: 'POST', body: form});
            const data = await res.json();

            const loading = document.getElementById('ocr-loading');
            if (loading) loading.remove();

            if (data.type === 'confirm' && data.expense) {
                renderConfirmCard(data.expense, data.categories || []);
            } else if (data.type === 'confirm_list' && data.items) {
                renderConfirmListCard(data);
            } else {
                messages.innerHTML += '<div class="chat-message bot">' + (data.response || '') + '</div>';
            }
        } catch (e) {
            const loading = document.getElementById('ocr-loading');
            if (loading) loading.remove();
            messages.innerHTML += '<div class="chat-message bot">사진 처리 중 오류가 발생했어요.</div>';
        }
        messages.scrollTop = messages.scrollHeight;
    }

    // 파싱 결과 확인 카드 (저장 전 사용자 확인/수정)
    function renderConfirmCard(expense, categories) {
        const messages = document.getElementById('messages');
        const n = chatCardSeq++;
        const id = 'cc' + n;
        pendingExpenses[n] = expense;
        lastPending = {type: 'single', n: n};
        if ((!userCategories || userCategories.length === 0) && categories) userCategories = categories;

        const labelStyle = 'display:block; font-size:11px; color:#666; margin:6px 0 2px;';
        const inputStyle = 'width:100%; margin:0; padding:6px; font-size:13px;';

        messages.innerHTML +=
            '<div class="chat-message bot" id="' + id + '" style="text-align:left;">' +
                '<div style="font-weight:600; margin-bottom:4px;">이렇게 저장할까요? <span style="font-weight:400; color:#888; font-size:12px;">(수정 가능)</span></div>' +
                '<label style="' + labelStyle + '">📂 종류</label>' +
                '<select id="' + id + '-kind" style="' + inputStyle + '">' +
                    '<option value="expense"' + (expense.kind === 'income' ? '' : ' selected') + '>지출</option>' +
                    '<option value="income"' + (expense.kind === 'income' ? ' selected' : '') + '>수입</option>' +
                '</select>' +
                '<label style="' + labelStyle + '">📅 날짜</label>' +
                '<input type="date" id="' + id + '-date" value="' + escapeHtml(expense.date) + '" style="' + inputStyle + '" />' +
                '<label style="' + labelStyle + '">🏪 가게</label>' +
                '<input id="' + id + '-store" value="' + escapeHtml(expense.store) + '" style="' + inputStyle + '" />' +
                '<label style="' + labelStyle + '">💰 금액</label>' +
                '<input type="number" id="' + id + '-amount" value="' + Number(expense.amount) + '" style="' + inputStyle + '" />' +
                '<label style="' + labelStyle + '">🏷️ 카테고리</label>' +
                '<select id="' + id + '-cat" style="' + inputStyle + '">' + catOptions(expense.category) + '</select>' +
                '<div style="display:flex; gap:5px; margin-top:5px;">' +
                    '<input id="' + id + '-newcat" placeholder="새 카테고리 추가" style="flex:1; margin:0; padding:6px; font-size:12px;" />' +
                    '<button onclick="addCatFromCard(' + n + ')" style="width:auto; margin:0; padding:6px 10px; font-size:12px; background:#4CAF50;">+</button>' +
                '</div>' +
                '<label style="' + labelStyle + '">🏦 계좌</label>' +
                '<select id="' + id + '-acc" style="' + inputStyle + '">' + accOptions(expense.account) + '</select>' +
                '<div style="display:flex; gap:5px; margin-top:8px;">' +
                    '<button onclick="confirmSave(' + n + ')" style="flex:1; margin:0; padding:8px; font-size:13px;">저장</button>' +
                    '<button onclick="cancelSave(' + n + ')" style="flex:1; margin:0; padding:8px; font-size:13px; background:#999;">취소</button>' +
                '</div>' +
                '<div style="font-size:11px; color:#aaa; margin-top:4px; text-align:center;">"응" / "취소" 라고 답해도 돼요</div>' +
            '</div>';
        messages.scrollTop = messages.scrollHeight;
    }

    // 확인 카드 안에서 새 카테고리 추가
    async function addCatFromCard(n) {
        const id = 'cc' + n;
        const input = document.getElementById(id + '-newcat');
        const name = input.value.trim();
        if (!name) { showToast('카테고리명을 입력하세요', 'error'); return; }

        const form = new FormData();
        form.append('name', name);
        try {
            const res = await fetch('/api/categories/add', {method: 'POST', body: form});
            const data = await res.json();
            if (data.status === 'success') {
                userCategories = data.categories;
                updateSelectOptions(data.categories);  // 사이드바 드롭다운도 갱신
                const sel = document.getElementById(id + '-cat');
                if (sel) sel.innerHTML = catOptions(name);  // 이 카드 드롭다운 갱신 + 새 값 선택
                input.value = '';
                showToast('카테고리 추가됨', 'success');
            } else {
                showToast(data.error || '카테고리 추가 실패', 'error');
            }
        } catch (e) { showToast('카테고리 추가 중 오류', 'error'); }
    }

    async function confirmSave(n) {
        if (!pendingExpenses[n]) return;
        if (lastPending && lastPending.n === n) lastPending = null;
        const id = 'cc' + n;
        const date = document.getElementById(id + '-date').value;
        const store = document.getElementById(id + '-store').value.trim();
        const amount = document.getElementById(id + '-amount').value;
        const category = document.getElementById(id + '-cat').value;
        const kind = document.getElementById(id + '-kind').value;
        const account = document.getElementById(id + '-acc').value;

        if (!date || !store || !amount) { showToast('날짜·가게·금액을 입력해주세요', 'error'); return; }
        if (parseInt(amount) <= 0) { showToast('금액은 1원 이상이어야 합니다', 'error'); return; }

        const form = new FormData();
        form.append('date', date);
        form.append('store', store);
        form.append('amount', parseInt(amount));
        form.append('category', category);
        form.append('account', account);
        form.append('kind', kind);

        try {
            const res = await fetch('/add/expense', {method: 'POST', body: form});
            const result = await res.json();
            const card = document.getElementById(id);
            if (result.status === 'success') {
                const kindLabel = kind === 'income' ? '수입' : '지출';
                if (card) card.outerHTML = '<div class="chat-message bot">✅ ' + kindLabel + ' 저장 완료: ' +
                    escapeHtml(date) + ' · ' + escapeHtml(store) + ' · ' +
                    Number(amount).toLocaleString() + '원 · ' + escapeHtml(kind === 'income' ? '수입' : category) + '</div>';
                delete pendingExpenses[n];
                // 저장한 날짜의 달로 달력 갱신
                const d = new Date(date);
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
        if (lastPending && lastPending.n === n) lastPending = null;
        const card = document.getElementById('cc' + n);
        if (card) card.outerHTML = '<div class="chat-message bot">취소했어요.</div>';
    }

    // 여러 날짜 한번에 저장하는 확인 카드
    const pendingMulti = {};
    function renderConfirmMultiCard(data, categories) {
        const messages = document.getElementById('messages');
        const n = chatCardSeq++;
        const id = 'cm' + n;
        pendingMulti[n] = data.dates;
        lastPending = {type: 'multi', n: n};
        if ((!userCategories || userCategories.length === 0) && categories) userCategories = categories;
        const e = data.expense;
        const labelStyle = 'display:block; font-size:11px; color:#666; margin:6px 0 2px;';
        const inputStyle = 'width:100%; margin:0; padding:6px; font-size:13px;';
        const datesPretty = data.dates.map(d => d.slice(5).replace('-', '/')).join(', ');

        messages.innerHTML +=
            '<div class="chat-message bot" id="' + id + '" style="text-align:left;">' +
                '<div style="font-weight:600; margin-bottom:4px;">' + data.dates.length + '개 날짜에 저장할까요? <span style="font-weight:400; color:#888; font-size:12px;">(수정 가능)</span></div>' +
                '<div style="font-size:12px; color:#555; margin-bottom:4px;">📅 ' + datesPretty + '</div>' +
                '<label style="' + labelStyle + '">📂 종류</label>' +
                '<select id="' + id + '-kind" style="' + inputStyle + '">' +
                    '<option value="expense"' + (e.kind === 'income' ? '' : ' selected') + '>지출</option>' +
                    '<option value="income"' + (e.kind === 'income' ? ' selected' : '') + '>수입</option>' +
                '</select>' +
                '<label style="' + labelStyle + '">🏪 가게/내용</label>' +
                '<input id="' + id + '-store" value="' + escapeHtml(e.store) + '" style="' + inputStyle + '" />' +
                '<label style="' + labelStyle + '">💰 금액 (각 날짜마다)</label>' +
                '<input type="number" id="' + id + '-amount" value="' + Number(e.amount) + '" style="' + inputStyle + '" />' +
                '<label style="' + labelStyle + '">🏷️ 카테고리</label>' +
                '<select id="' + id + '-cat" style="' + inputStyle + '">' + catOptions(e.category) + '</select>' +
                '<label style="' + labelStyle + '">🏦 계좌</label>' +
                '<select id="' + id + '-acc" style="' + inputStyle + '">' + accOptions(userAccounts[0]) + '</select>' +
                '<div style="display:flex; gap:5px; margin-top:8px;">' +
                    '<button onclick="confirmSaveMulti(' + n + ')" style="flex:1; margin:0; padding:8px; font-size:13px;">' + data.dates.length + '건 저장</button>' +
                    '<button onclick="cancelMulti(' + n + ')" style="flex:1; margin:0; padding:8px; font-size:13px; background:#999;">취소</button>' +
                '</div>' +
                '<div style="font-size:11px; color:#aaa; margin-top:4px; text-align:center;">"응" / "취소" 라고 답해도 돼요</div>' +
            '</div>';
        messages.scrollTop = messages.scrollHeight;
    }

    async function confirmSaveMulti(n) {
        const dates = pendingMulti[n];
        if (!dates) return;
        if (lastPending && lastPending.n === n) lastPending = null;
        const id = 'cm' + n;
        const store = document.getElementById(id + '-store').value.trim();
        const amount = document.getElementById(id + '-amount').value;
        const category = document.getElementById(id + '-cat').value;
        const kind = document.getElementById(id + '-kind').value;
        const account = document.getElementById(id + '-acc').value;
        if (!store || !amount) { showToast('가게/내용과 금액을 입력해주세요', 'error'); return; }
        if (parseInt(amount) <= 0) { showToast('금액은 1원 이상이어야 합니다', 'error'); return; }

        let ok = 0;
        for (const date of dates) {
            const form = new FormData();
            form.append('date', date);
            form.append('store', store);
            form.append('amount', parseInt(amount));
            form.append('category', category);
            form.append('kind', kind);
            form.append('account', account);
            try {
                const res = await fetch('/add/expense', {method: 'POST', body: form});
                const result = await res.json();
                if (result.status === 'success') ok++;
            } catch (e) { /* 계속 */ }
        }
        const card = document.getElementById(id);
        if (card) card.outerHTML = '<div class="chat-message bot">✅ ' + ok + '건 저장 완료: ' +
            escapeHtml(store) + ' · ' + Number(amount).toLocaleString() + '원 (' +
            (kind === 'income' ? '수입' : escapeHtml(category)) + ')</div>';
        delete pendingMulti[n];
        if (dates.length) {
            const d = new Date(dates[dates.length - 1]);
            currentMonth = new Date(d.getFullYear(), d.getMonth(), 1);
            loadExpensesAndRender();
        }
        document.getElementById('messages').scrollTop = 1e9;
    }

    function cancelMulti(n) {
        delete pendingMulti[n];
        if (lastPending && lastPending.n === n) lastPending = null;
        const card = document.getElementById('cm' + n);
        if (card) card.outerHTML = '<div class="chat-message bot">취소했어요.</div>';
    }

    // 통장/카드 여러 건(각기 다른 거래) 확인 리스트
    const pendingLists = {};
    function renderConfirmListCard(data) {
        const messages = document.getElementById('messages');
        const n = chatCardSeq++;
        const items = data.items || [];
        pendingLists[n] = items;
        if ((!userCategories || userCategories.length === 0) && data.categories) userCategories = data.categories;

        let rows = '';
        items.forEach((it, i) => {
            rows += '<div id="row-' + n + '-' + i + '" style="border-bottom:1px solid #eee; padding:6px 0;">' +
                '<div style="display:flex; justify-content:space-between; align-items:center; gap:4px;">' +
                    '<span style="font-size:11px; color:#888; white-space:nowrap;">' + escapeHtml(it.date.slice(5)) + '</span>' +
                    '<select id="li' + n + '_' + i + '_kind" style="width:auto; margin:0; padding:3px; font-size:11px;">' +
                        '<option value="expense"' + (it.kind === 'income' ? '' : ' selected') + '>지출</option>' +
                        '<option value="income"' + (it.kind === 'income' ? ' selected' : '') + '>수입</option>' +
                    '</select>' +
                    '<button onclick="removeListItem(' + n + ',' + i + ')" style="width:auto; margin:0; padding:2px 8px; font-size:11px; background:#f44336;">✕</button>' +
                '</div>' +
                '<div style="display:flex; gap:4px; margin-top:4px;">' +
                    '<input id="li' + n + '_' + i + '_store" value="' + escapeHtml(it.store) + '" style="flex:2; margin:0; padding:4px; font-size:12px; min-width:0;" />' +
                    '<input id="li' + n + '_' + i + '_amount" type="number" value="' + Number(it.amount) + '" style="flex:1; margin:0; padding:4px; font-size:12px; min-width:0;" />' +
                '</div>' +
            '</div>';
        });

        messages.innerHTML +=
            '<div class="chat-message bot" id="cl' + n + '" style="text-align:left;">' +
                '<div style="font-weight:600; margin-bottom:6px;">' + items.length + '건을 읽었어요 <span style="font-weight:400; color:#888; font-size:12px;">(수정/✕삭제 후 저장)</span></div>' +
                '<div style="display:flex; align-items:center; gap:6px; margin-bottom:6px;"><span style="font-size:12px; color:#666;">🏦 계좌</span>' +
                    '<select id="cl' + n + '_acc" style="flex:1; margin:0; padding:5px; font-size:12px;">' + accOptions(userAccounts[0]) + '</select></div>' +
                '<div style="max-height:280px; overflow-y:auto; overscroll-behavior:contain;">' + rows + '</div>' +
                '<div style="display:flex; gap:5px; margin-top:8px;">' +
                    '<button onclick="confirmSaveList(' + n + ')" style="flex:1; margin:0; padding:8px; font-size:13px;">저장</button>' +
                    '<button onclick="cancelList(' + n + ')" style="flex:1; margin:0; padding:8px; font-size:13px; background:#999;">취소</button>' +
                '</div>' +
            '</div>';
        messages.scrollTop = messages.scrollHeight;
    }

    function removeListItem(n, i) {
        const r = document.getElementById('row-' + n + '-' + i);
        if (r) r.remove();
    }

    async function confirmSaveList(n) {
        const items = pendingLists[n];
        if (!items) return;
        const accEl = document.getElementById('cl' + n + '_acc');
        const account = accEl ? accEl.value : '';
        let ok = 0;
        for (let i = 0; i < items.length; i++) {
            const row = document.getElementById('row-' + n + '-' + i);
            if (!row) continue;  // ✕로 삭제된 항목
            const store = document.getElementById('li' + n + '_' + i + '_store').value.trim();
            const amount = document.getElementById('li' + n + '_' + i + '_amount').value;
            const kind = document.getElementById('li' + n + '_' + i + '_kind').value;
            if (!store || !amount || parseInt(amount) <= 0) continue;
            const form = new FormData();
            form.append('date', items[i].date);
            form.append('store', store);
            form.append('amount', parseInt(amount));
            form.append('category', items[i].category || (userCategories[0] || '기타'));
            form.append('kind', kind);
            form.append('account', account);
            try {
                const res = await fetch('/add/expense', {method: 'POST', body: form});
                const r = await res.json();
                if (r.status === 'success') ok++;
            } catch (e) { /* 계속 */ }
        }
        const card = document.getElementById('cl' + n);
        if (card) card.outerHTML = '<div class="chat-message bot">✅ ' + ok + '건 저장 완료</div>';
        delete pendingLists[n];
        loadExpensesAndRender();
        document.getElementById('messages').scrollTop = 1e9;
    }

    function cancelList(n) {
        delete pendingLists[n];
        const card = document.getElementById('cl' + n);
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
                userCategories = data.categories;
                updateSelectOptions(data.categories);
                if (document.getElementById('categoryList')) {
                    renderCategoryList(data.categories);
                }
            }
        } catch (e) {
            console.log('카테고리 로드 중 오류:', e);
        }
    }

    let catModalList = [];
    function renderCategoryList(categories) {
        catModalList = categories;
        const list = document.getElementById('categoryList');
        list.innerHTML = categories.map((cat, idx) => `
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px; background: #f9f9f9; border-radius: 6px; margin-bottom: 8px;">
                <span id="cat-display-${idx}" style="flex: 1;">${escapeHtml(cat)}</span>
                <div id="cat-edit-${idx}" style="display: none; flex: 1; display: flex; gap: 5px;">
                    <input type="text" id="cat-input-${idx}" value="${escapeHtml(cat)}" style="flex: 1; padding: 5px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px;" />
                    <button onclick="saveEdit(${idx})" style="padding: 5px 10px; width: auto; background: #4CAF50; font-size: 12px;">✓</button>
                    <button onclick="cancelEdit(${idx})" style="padding: 5px 10px; width: auto; background: #999; font-size: 12px;">✕</button>
                </div>
                <button onclick="startEdit(${idx})" style="padding: 5px 10px; width: auto; background: #FF9800; font-size: 12px; margin-left: 5px;">수정</button>
                <button onclick="deleteCategory(${idx})" style="padding: 5px 10px; width: auto; background: #f44336; font-size: 12px; margin-left: 5px;">삭제</button>
            </div>
        `).map((html, idx) => html.replace('display: none; flex: 1; display: flex;', 'display: none; flex: 1;')).join('');
    }

    let editingIndex = null;

    function startEdit(idx) {
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

    async function deleteCategory(idx) {
        const name = catModalList[idx];
        if (name === undefined) return;
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

    // 계좌 관리 함수들
    let accModalList = [];
    async function loadAccounts() {
        try {
            const res = await fetch('/api/accounts');
            const data = await res.json();
            if (data.accounts) {
                userAccounts = data.accounts;
                updateAccountOptions(data.accounts);
                if (document.getElementById('accountList')) renderAccountList(data.accounts);
            }
        } catch (e) { console.log('계좌 로드 오류:', e); }
    }
    function updateAccountOptions(accounts) {
        const sel = document.getElementById('account');
        if (!sel) return;
        sel.innerHTML = '';
        accounts.forEach(a => { const o = document.createElement('option'); o.textContent = a; sel.appendChild(o); });
    }
    function accOptions(selected) {
        let accs = userAccounts.slice();
        if (selected && accs.indexOf(selected) === -1) accs = [selected].concat(accs);
        return accs.map(a => '<option value="' + escapeHtml(a) + '"' + (a === selected ? ' selected' : '') + '>' + escapeHtml(a) + '</option>').join('');
    }
    function renderAccountList(accounts) {
        accModalList = accounts;
        const list = document.getElementById('accountList');
        if (!list) return;
        list.innerHTML = accounts.map((a, idx) =>
            '<div style="display:flex; justify-content:space-between; align-items:center; padding:10px; background:#f9f9f9; border-radius:6px; margin-bottom:8px;">' +
                '<span style="flex:1;">' + escapeHtml(a) + '</span>' +
                '<button onclick="deleteAccount(' + idx + ')" style="padding:5px 10px; width:auto; background:#f44336; font-size:12px;">삭제</button>' +
            '</div>'
        ).join('') || '<div style="color:#999; font-size:13px;">계좌가 없어요. 추가해보세요.</div>';
    }
    function openAccountModal() {
        const m = document.getElementById('accountModal');
        m.style.display = 'flex';
        loadAccounts();
        setTimeout(() => { const el = document.getElementById('newAccountName'); if (el) el.focus(); }, 100);
    }
    function closeAccountModal() { document.getElementById('accountModal').style.display = 'none'; }
    async function addNewAccount() {
        const name = document.getElementById('newAccountName').value.trim();
        if (!name) { showToast('계좌명을 입력해주세요', 'error'); return; }
        const form = new FormData(); form.append('name', name);
        const res = await fetch('/api/accounts/add', {method: 'POST', body: form});
        const data = await res.json();
        if (data.status === 'success') {
            document.getElementById('newAccountName').value = '';
            userAccounts = data.accounts;
            renderAccountList(data.accounts); updateAccountOptions(data.accounts);
        } else showToast(data.error || '오류가 발생했습니다', 'error');
    }
    async function deleteAccount(idx) {
        const name = accModalList[idx];
        if (name === undefined) return;
        const form = new FormData(); form.append('name', name);
        const res = await fetch('/api/accounts/delete', {method: 'POST', body: form});
        const data = await res.json();
        if (data.status === 'success') {
            userAccounts = data.accounts;
            renderAccountList(data.accounts); updateAccountOptions(data.accounts);
        } else showToast(data.error || '오류가 발생했습니다', 'error');
    }
    document.addEventListener('DOMContentLoaded', function() {
        const m = document.getElementById('accountModal');
        if (m) m.addEventListener('click', function(e) { if (e.target === m) closeAccountModal(); });
    });

    // ── 첫 접속 온보딩 (은행 등록) ──
    const COMMON_BANKS = ["카카오뱅크", "토스뱅크", "국민은행", "신한은행", "우리은행", "하나은행", "농협", "케이뱅크", "현금", "신용카드"];
    async function checkOnboarding() {
        try {
            const res = await fetch('/api/me');
            const data = await res.json();
            if (data.accounts) userAccounts = data.accounts;
            if (!data.onboarded) openOnboarding();
        } catch (e) {}
    }
    function openOnboarding() {
        const box = document.getElementById('bankChips');
        box.innerHTML = COMMON_BANKS.map((b, i) =>
            '<button onclick="onboardAddQuick(' + i + ')" style="width:auto; margin:0; padding:6px 12px; font-size:13px; background:#eef3f9; color:#333; border:1px solid #cfe0f2;">' + escapeHtml(b) + '</button>'
        ).join('');
        renderOnboardList();
        document.getElementById('onboardModal').style.display = 'flex';
    }
    async function onboardAddQuick(i) { await onboardAdd(COMMON_BANKS[i]); }
    async function onboardAddCustom() {
        const el = document.getElementById('onboardInput');
        const v = el.value.trim();
        if (!v) return;
        el.value = '';
        await onboardAdd(v);
    }
    async function onboardAdd(name) {
        const form = new FormData(); form.append('name', name);
        const res = await fetch('/api/accounts/add', {method: 'POST', body: form});
        const data = await res.json();
        if (data.status === 'success') {
            userAccounts = data.accounts;
            updateAccountOptions(data.accounts);
            renderOnboardList();
        } else if (!(data.error && data.error.indexOf('이미') !== -1)) {
            showToast(data.error || '오류가 발생했습니다', 'error');
        }
    }
    function renderOnboardList() {
        const list = document.getElementById('onboardList');
        if (!list) return;
        list.innerHTML = (userAccounts || []).map((a, idx) =>
            '<div style="display:flex; justify-content:space-between; align-items:center; padding:8px 10px; background:#f9f9f9; border-radius:6px; margin-bottom:6px;">' +
                '<span>' + escapeHtml(a) + '</span>' +
                '<button onclick="onboardDelete(' + idx + ')" style="width:auto; margin:0; padding:3px 10px; font-size:12px; background:#f44336;">삭제</button>' +
            '</div>'
        ).join('') || '<div style="color:#999; font-size:13px;">위에서 은행을 추가해보세요.</div>';
    }
    async function onboardDelete(idx) {
        const name = userAccounts[idx];
        if (name === undefined) return;
        const form = new FormData(); form.append('name', name);
        const res = await fetch('/api/accounts/delete', {method: 'POST', body: form});
        const data = await res.json();
        if (data.status === 'success') {
            userAccounts = data.accounts;
            updateAccountOptions(data.accounts);
            renderOnboardList();
        }
    }
    async function onboardComplete() {
        try { await fetch('/api/onboard/complete', {method: 'POST'}); } catch (e) {}
        document.getElementById('onboardModal').style.display = 'none';
        loadAccounts();
    }

    document.getElementById('date').valueAsDate = new Date();
    loadExpensesAndRender();
    loadCategories();
    loadAccounts();
    checkOnboarding();
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

# ── 사용자 상태 / 온보딩 ────────────────────────────
@app.get("/api/me")
async def get_me(session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    accounts = user.get("accounts", DEFAULT_ACCOUNTS)
    # 온보딩 완료 플래그가 없고 계좌도 기본 상태면 온보딩 필요
    onboarded = bool(user.get("onboarded")) or len(accounts) > 1
    return {"username": user.get("username"), "onboarded": onboarded, "accounts": accounts}

@app.post("/api/onboard/complete")
async def complete_onboard(session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"onboarded": True}})
    return {"status": "success"}

# ── 계좌(은행/카드) 관리 API ────────────────────────
@app.get("/api/accounts")
async def get_accounts(session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    return {"accounts": user.get("accounts", DEFAULT_ACCOUNTS)}

@app.post("/api/accounts/add")
async def add_account(name: str = Form(...), session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    name = name.strip()
    if not name:
        return JSONResponse({"error": "계좌명을 입력해주세요"}, status_code=400)
    accounts = user.get("accounts", DEFAULT_ACCOUNTS)
    if name in accounts:
        return JSONResponse({"error": "이미 존재하는 계좌입니다"}, status_code=400)
    accounts.append(name)
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"accounts": accounts}})
    return {"status": "success", "accounts": accounts}

@app.post("/api/accounts/delete")
async def delete_account(name: str = Form(...), session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    accounts = user.get("accounts", DEFAULT_ACCOUNTS)
    if name not in accounts:
        return JSONResponse({"error": "존재하지 않는 계좌입니다"}, status_code=400)
    accounts.remove(name)
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"accounts": accounts}})
    return {"status": "success", "accounts": accounts}

@app.post("/api/accounts/rename")
async def rename_account(old_name: str = Form(...), new_name: str = Form(...), session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    new_name = new_name.strip()
    accounts = user.get("accounts", DEFAULT_ACCOUNTS)
    if old_name not in accounts:
        return JSONResponse({"error": "존재하지 않는 계좌입니다"}, status_code=400)
    if not new_name or new_name in accounts:
        return JSONResponse({"error": "잘못된 계좌명입니다"}, status_code=400)
    accounts[accounts.index(old_name)] = new_name
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"accounts": accounts}})
    # 기존 내역의 계좌명도 함께 변경
    await db.expenses.update_many({"user_id": user["_id"], "account": old_name}, {"$set": {"account": new_name}})
    return {"status": "success", "accounts": accounts}

# ── 위시리스트 API ─────────────────────────────────
def _coupang_search_url(name: str) -> str:
    return "https://www.coupang.com/np/search?q=" + quote(name)

def _naver_shop_search(query: str, display: int = 6):
    """네이버 쇼핑 검색 → [{title, price, image, link, mall}]. 자격증명 없으면 예외."""
    url = "https://openapi.naver.com/v1/search/shop.json?query=" + quote(query) + f"&display={display}&sort=sim"
    req = Request(url, headers={
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    })
    with urlopen(req, timeout=8) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    items = []
    for it in data.get("items", []):
        title = re.sub(r"<[^>]+>", "", it.get("title", "")).strip()  # <b> 태그 제거
        try:
            price = int(it.get("lprice", 0))
        except (ValueError, TypeError):
            price = 0
        items.append({
            "title": title,
            "price": price,
            "image": it.get("image", ""),
            "link": it.get("link", ""),
            "mall": it.get("mallName", ""),
        })
    return items

@app.get("/api/wishlist/search")
async def search_wishlist(q: str, session: Optional[str] = Cookie(None)):
    """제품명으로 네이버 쇼핑 검색 → 가격/이미지/링크 후보 반환"""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return JSONResponse({"error": "네이버 검색 API 키가 필요해요. developers.naver.com 에서 발급 후 .env 에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 를 넣어주세요."}, status_code=400)
    q = (q or "").strip()
    if not q:
        return {"items": []}
    try:
        items = await asyncio.to_thread(_naver_shop_search, q)
    except Exception as ex:
        return JSONResponse({"error": f"검색 중 오류가 발생했어요: {ex}"}, status_code=502)
    return {"items": items}

@app.get("/api/wishlist")
async def get_wishlist(session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    items = []
    async for doc in db.wishlist.find({"user_id": user["_id"]}).sort("order", 1):
        doc["_id"] = str(doc["_id"])
        doc.pop("user_id", None)
        items.append(doc)
    return {"items": items}

@app.post("/api/wishlist/add")
async def add_wishlist(
    name: str = Form(...),
    price: int = Form(...),
    image_url: str = Form(""),
    url: str = Form(""),
    session: Optional[str] = Cookie(None),
):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    name = name.strip()
    if not name:
        return JSONResponse({"error": "제품명을 입력해주세요"}, status_code=400)
    if price < 0:
        return JSONResponse({"error": "가격이 올바르지 않습니다"}, status_code=400)

    # 맨 뒤에 추가 (order = 현재 최대 + 1)
    last = await db.wishlist.find_one({"user_id": user["_id"]}, sort=[("order", -1)])
    order = (last["order"] + 1) if last and "order" in last else 0

    doc = {
        "user_id": user["_id"],
        "name": name,
        "price": price,
        "url": url.strip() or _coupang_search_url(name),  # 검색 선택 링크 있으면 사용, 없으면 쿠팡 검색
        "image_url": image_url.strip(),
        "order": order,
        "created_at": datetime.now().isoformat(),
    }
    result = await db.wishlist.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    doc.pop("user_id", None)
    return {"status": "success", "item": doc}

@app.post("/api/wishlist/delete")
async def delete_wishlist(id: str = Form(...), session: Optional[str] = Cookie(None)):
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    try:
        oid = ObjectId(id)
    except InvalidId:
        return JSONResponse({"error": "잘못된 id입니다"}, status_code=400)
    result = await db.wishlist.delete_one({"_id": oid, "user_id": user["_id"]})
    if result.deleted_count == 0:
        return JSONResponse({"error": "항목을 찾을 수 없습니다"}, status_code=404)
    return {"status": "success"}

@app.post("/api/wishlist/reorder")
async def reorder_wishlist(ids: str = Form(...), session: Optional[str] = Cookie(None)):
    """ids: 새 순서의 id 배열(JSON 문자열). 각 항목 order를 인덱스로 갱신."""
    user = await get_current_user(session)
    if not user:
        return JSONResponse({"error": "로그인 필요"}, status_code=401)
    try:
        id_list = _json.loads(ids)
    except Exception:
        return JSONResponse({"error": "잘못된 순서 데이터"}, status_code=400)
    for idx, sid in enumerate(id_list):
        try:
            oid = ObjectId(sid)
        except InvalidId:
            continue
        await db.wishlist.update_one(
            {"_id": oid, "user_id": user["_id"]},
            {"$set": {"order": idx}},
        )
    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
