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
python3 -m venv venv # 가상환경 생성
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```
##
docker-compose up -d
open http://localhost:8000/docs

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

1단계: 웹에서 입력 → DB 저장        ← 지금 여기
2단계: 저장된 데이터 조회/시각화
3단계: LLM으로 영수증 자동 파싱
4단계: 카테고리 자동 분류
5단계: RAG 챗봇 ("이번달 식비 얼마야?")
6단계: Airflow 자동화 파이프라인
7단계: 앱으로 전환 (React Native)
8단계: AWS 배포