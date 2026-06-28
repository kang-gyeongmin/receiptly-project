# Receiptly

영수증 기반 AI 가계부 앱 — 자연어로 입력하면 자동으로 분류하는 똑똑한 가계부

## 🎯 핵심 기능

- **🤖 자연어 파싱** ✅: `"6/20 서브웨이 8000"` → 자동으로 날짜/가게/금액 추출
- **📸 영수증 OCR** ✅: 결제내역 사진 → 텍스트 추출 + 자동 파싱
- **🏷️ 카테고리 자동분류** ✅: 음식/교통/쇼핑 등으로 자동 분류

## Phase 진행 상황

- [x] **Phase 1** — 기본 뼈대 (텍스트/이미지 입력, MongoDB 저장)
- [x] **Phase 2** — AI 기능 추가 (완료!)
  - [x] Step 1: 자연어 파싱 (정규식)
  - [x] Step 2: 영수증 OCR (EasyOCR) 
  - [x] Step 3: 카테고리 자동분류 (키워드 매칭)
- [ ] Phase 3 — 데이터 고도화 (통계, RAG 챗봇)
- [ ] Phase 4 — 배포 (Docker, AWS)

## 🚀 실행 방법

### 로컬 개발

```bash
# 1. 의존성 설치
pip install -r requirements.txt

# 2. MongoDB 시작 (Docker)
docker-compose up -d

# 3. 서버 실행
uvicorn main:app --reload
```

**웹 브라우저에서 열기**:
```bash
open http://localhost:8000
# 또는
open http://localhost:8000/docs (FastAPI 자동 문서)
```

### 환경변수 (.env)
```bash
MONGO_URI=mongodb://localhost:27017
DB_NAME=receiptly
```

## 📝 사용 예시

### 1️⃣ 자연어 입력 (가장 빠름)
```
웹: "6/20 서브웨이 8000" 입력 → 자동 파싱 + 분류 + 저장

API: POST /add/parsed
     Body: text="금요일 카페 5500"
     Response: {
       "status": "saved",
       "parsed": {
         "date": "2024-06-21",
         "store_name": "카페",
         "amount": 5500,
         "category": "음식"  ← 자동분류!
       }
     }
```

### 2️⃣ 영수증 촬영 (OCR)
```
결제내역 사진 업로드
  ↓
EasyOCR로 텍스트 추출
  ↓
자연어 파싱 + 카테고리 분류
  ↓
자동 저장
```

**예시 OCR 인식**:
```
입력: 영수증 이미지
출력:
  추출된 텍스트: "스타벅스 아메리카노 5000원"
  파싱 결과:
    - 가게: 스타벅스
    - 금액: 5000
    - 카테고리: 음식
```

### 3️⃣ 직접 입력
```
가게명: 서브웨이
금액: 8000
날짜: 2024-06-20
메모: 점심식사
  ↓
카테고리 자동분류: "음식"
  ↓
저장
```

## 🔌 API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/parse/text` | 자연어 텍스트 파싱 (DB 미저장) |
| POST | `/add/parsed` | 자연어 파싱 후 저장 |
| POST | `/add/text` | 폼 입력으로 저장 |
| POST | `/add/image` | 이미지 업로드 |
| GET | `/list` | 저장된 내역 조회 |
| GET | `/list/images` | 업로드된 이미지 목록 |

## 🛠️ 기술 스택

| 계층 | 기술 | 용도 |
|------|------|------|
| Backend | FastAPI | REST API |
| DB | MongoDB | 문서 저장 |
| NLP | 정규식 + spaCy | 자연어 파싱 |
| OCR | EasyOCR | 영수증 텍스트 추출 (Step 2) |
| 분류 | 키워드 매칭 | 카테고리 자동분류 (Step 3) |

## 📚 다음 단계

### Step 2: 영수증 OCR (예정)
- EasyOCR로 이미지 → 텍스트 추출
- 파싱 후 자동 저장

### Step 3: 카테고리 자동분류 (예정)
- 키워드 기반 분류 규칙
- 사용자가 정의하는 분류 카테고리

### Phase 3: 데이터 분석
- 월별/카테고리별 통계
- RAG 챗봇 ("이번달 식비 얼마?")

## 📌 주의사항

- 자연어 파싱은 간단한 패턴만 지원 (복잡한 형식은 직접 입력 권장)
- 영수증 OCR은 한글 지원하지만 정확도는 이미지 품질에 따라 다름
- 모든 기능은 **완전 무료** (Claude API 불필요)