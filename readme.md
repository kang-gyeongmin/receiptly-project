# Receiptly

영수증 기반 AI 가계부 앱

## Phase 진행 상황

- [x] Phase 1 — 뼈대 (텍스트 입력, 목록 조회, MongoDB)
- [ ] Phase 2 — AI 붙이기 (OCR, LLM 파싱, 카메라)
- [ ] Phase 3 — 데이터 고도화 (PostgreSQL, Polars, RAG 챗봇)
- [ ] Phase 4 — 자동화 + 배포 (Airflow, SageMaker, AWS, Xcode)

## 실행 방법

### 백엔드
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

### 앱
```bash
cd app
npm install
npx expo start
```

## 환경변수 (.env)
```
MONGO_URI=mongodb://localhost:27017
DB_NAME=receiptly
```