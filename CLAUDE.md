# Receiptly — 개발 가이드

## 프로젝트 개요

자연어 입력, 영수증 OCR, 자동 분류를 통해 가계부를 쉽게 관리하는 AI 기반 앱.

**현재 상태**: Phase 2 진행 중 (AI 기능 추가)  
**기술 스택**: FastAPI + MongoDB + 무료 오픈소스 라이브러리

---

## 구조

```
receiptly-project/
├── main.py                 # FastAPI 백엔드 + 웹 UI
├── requirements.txt        # 의존성
├── docker-compose.yml      # MongoDB 로컬 개발
├── .env                    # 환경변수 (git 무시)
└── README.md              # 사용자 문서 (지금 읽는 것)
```

**구조 특징**:
- 모놀리식 (main.py 하나): 프로토타입 단계이므로 간단하게 유지
- 프론트엔드 없음: HTML/JS로 간단한 웹 UI만 제공
- 앱은 별도 (Phase 4에서 React Native로 전환 예정)

---

## 개발 규칙

### 1. 파싱 로직 추가할 때

자연어 파싱은 `parse_natural_language()` 함수에서만 수정합니다.

```python
# ❌ 금지: 엔드포인트에서 직접 파싱
@app.post("/add/parsed")
async def add_parsed(text: str):
    amount = int(text.split()[-1])  # 이렇게 하면 안됨

# ✅ 권장: 함수에서 처리
def parse_natural_language(text: str) -> dict:
    # 여기서만 수정
```

**이유**: 같은 로직을 여러 곳에서 재사용하기 위함 (API, 웹 UI, 나중에 앱까지)

### 2. OCR / 카테고리 분류 추가할 때

Phase 2의 Step 2/3을 구현할 때:

```python
# Step 2: OCR
def extract_text_from_image(image_bytes: bytes) -> str:
    """EasyOCR로 이미지에서 텍스트 추출"""
    # 여기서만 구현

# Step 3: 카테고리 분류
def classify_expense(store_name: str, amount: int) -> str:
    """
    가게명/금액으로 카테고리 반환
    Returns: "음식", "교통", "쇼핑", "의료", "기타"
    """
    # 여기서만 구현
```

### 3. DB 스키마 변경할 때

MongoDB는 스키마가 느슨하지만, 새 필드 추가 후 코드에 문서를 남깁니다.

```python
# 예: 카테고리 필드 추가할 때
doc = {
    "type": "parsed",
    "date": "2024-06-20",
    "store_name": "서브웨이",
    "amount": 8000,
    "memo": "",
    "category": "음식",  # ← 새로 추가된 필드
    "created_at": datetime.utcnow().isoformat()
}
```

### 4. 환경변수 추가할 때

`.env` 파일에 추가하고, `main.py` 상단에서 로드합니다.

```bash
# .env
MONGO_URI=mongodb://localhost:27017
DB_NAME=receiptly
# NEW_VAR=value 추가
```

```python
# main.py
load_dotenv()
# os.getenv("NEW_VAR")로 사용
```

---

## Phase 2 구현 완료! ✅

### Step 1: 자연어 파싱 ✅
- [x] 정규식으로 날짜/가게/금액 추출
- [x] `/parse/text` API 엔드포인트
- [x] `/add/parsed` 저장 엔드포인트
- [x] 웹 UI에 자연어 입력란 추가
- [x] 모든 저장 함수에 카테고리 필드 추가

### Step 2: 영수증 OCR ✅
- [x] EasyOCR로 이미지 → 텍스트 추출
- [x] `/add/image` 엔드포인트 OCR 기능 추가
- [x] OCR 결과 → 자연어 파싱
- [x] OCR 결과를 웹에서 확인 가능

**구현 코드**:
```python
def extract_text_from_image(image_bytes: bytes) -> str:
    """EasyOCR로 이미지에서 한글 텍스트 추출"""
    if reader is None:
        return ""
    
    try:
        image = Image.open(BytesIO(image_bytes))
        results = reader.readtext(image)
        extracted_text = " ".join([result[1] for result in results])
        return extracted_text
    except Exception as e:
        print(f"OCR 오류: {e}")
        return ""
```

### Step 3: 카테고리 자동분류 ✅
- [x] 키워드 기반 분류 규칙
- [x] 모든 거래에 자동 분류
- [x] 웹 UI에 카테고리 표시

**분류 규칙**:
```python
CATEGORIES = {
    "음식": ["카페", "식당", "서브웨이", "맥도날드", "편의점", ...],
    "교통": ["택시", "버스", "지하철", "주유소", ...],
    "쇼핑": ["마트", "쿠팡", "의류", "신발", ...],
    "의료": ["약국", "병원", "치과", ...],
    "엔터": ["영화", "극장", ...],
    "주거": ["아파트", "월세", "전기", "가스", ...],
}

def classify_expense(store_name: str, memo: str = "") -> str:
    """가게명/메모로 카테고리 자동 분류"""
    search_text = (store_name + " " + memo).lower()
    
    for category, keywords in CATEGORIES.items():
        if category == "기타":
            continue
        for keyword in keywords:
            if keyword in search_text:
                return category
    
    return "기타"
```

---

## 로컬 개발 환경 셋업

### 1. MongoDB 시작
```bash
docker-compose up -d
# MongoDB가 localhost:27017에서 실행됨
```

### 2. 가상환경 + 의존성
```bash
python3 -m venv venv
source venv/bin/activate  # macOS/Linux
# 또는
venv\Scripts\activate     # Windows

pip install -r requirements.txt
```

### 3. 서버 실행
```bash
uvicorn main:app --reload
```

**접속**:
- 웹 UI: http://localhost:8000
- API 문서: http://localhost:8000/docs
- 데이터 조회: http://localhost:8000/list

---

## 테스트하기

### 자연어 파싱 테스트

```bash
# API로 테스트
curl -X POST "http://localhost:8000/parse/text?text=6/20%20서브웨이%208000"

# 또는 웹 UI에서 직접 입력
```

**테스트 케이스**:
```
"6/20 서브웨이 8000" → date: 2024-06-20, store: 서브웨이, amount: 8000
"금요일 카페 5500" → date: 오늘 날짜, store: 카페, amount: 5500
"3000" → amount: 3000, 다른 필드는 None (에러 반환)
```

---

## 문제해결

### MongoDB 연결 안 될 때
```bash
# Docker 실행 확인
docker ps | grep mongodb

# 재시작
docker-compose restart
```

### 포트 충돌 (8000 이미 사용)
```bash
uvicorn main:app --reload --port 8001
```

### 라이브러리 import 에러
```bash
# 가상환경 활성화 확인
which python  # /path/to/venv/bin/python 이어야 함

# 재설치
pip install -r requirements.txt --force-reinstall
```

---

## 다음 사람을 위한 메모

- **대규모 리팩토링 예정**: Phase 3에서 backends/databases/models 등으로 분리할 계획
- **OCR 모델 선택**: EasyOCR 성능 확인 후 필요시 다른 모델로 변경 가능 (PaddleOCR, Tesseract)
- **카테고리 확장**: 나중에 사용자가 직접 추가/수정할 수 있도록 DB 스키마 설계
- **한글 처리**: spaCy 한글 모델은 최소한이므로 필요하면 KoNLPy 고려

---

## 참고자료

- **FastAPI**: https://fastapi.tiangolo.com/
- **Motor (MongoDB async)**: https://motor.readthedocs.io/
- **EasyOCR**: https://github.com/JaidedAI/EasyOCR
- **Python regex**: https://docs.python.org/3/library/re.html
