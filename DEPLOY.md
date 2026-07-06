# 배포 가이드 (MongoDB Atlas + Hugging Face Spaces)

무료로 배포해서 폰·다른 사람도 쓸 수 있게 하기. OCR 포함.

---

## 1단계: MongoDB Atlas (클라우드 DB) — 먼저

1. https://www.mongodb.com/cloud/atlas 가입 (구글 로그인 가능)
2. **무료 M0 클러스터** 생성 (지역: Seoul 또는 가까운 곳)
3. 생성 과정에서:
   - **Database User** 만들기 → 아이디/비번 메모 (DB 접속용, 사이트 로그인과 별개)
   - **Network Access** → **Allow access from anywhere (0.0.0.0/0)** 추가
     (호스팅 IP가 고정이 아니라 전체 허용 필요)
4. 클러스터 생성 후 **Connect → Drivers → Python** → **연결 문자열** 복사
   - 예: `mongodb+srv://<아이디>:<비번>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`
   - `<비번>` 자리에 실제 비번을 넣되, 비번에 특수문자 있으면 URL 인코딩 필요

➡️ **이 연결 문자열을 준비** (2단계 secrets에 넣음)

---

## 2단계: Hugging Face Spaces (호스팅, OCR 포함)

1. https://huggingface.co 가입
2. **New Space** 생성
   - Owner: 본인 / Space name: `receiptly`
   - **SDK: Docker** 선택 (중요! blank template)
   - 공개/비공개 선택 (무료 둘 다 됨)
3. **Settings → Variables and secrets** 에서 아래를 **Secret**으로 추가:
   | 이름 | 값 |
   |------|-----|
   | `MONGO_URI` | (1단계 Atlas 연결 문자열) |
   | `DB_NAME` | `receiptly` |
   | `ODSAY_API_KEY` | (기존 .env 값) |
   | `NAVER_CLIENT_ID` | (기존 .env 값) |
   | `NAVER_CLIENT_SECRET` | (기존 .env 값) |
4. **코드 올리기** (터미널):
   ```bash
   # HF 액세스 토큰 발급: huggingface.co/settings/tokens (write 권한)
   git remote add hf https://<HF아이디>:<HF토큰>@huggingface.co/spaces/<HF아이디>/receiptly
   git push --force hf main
   ```
5. HF가 자동으로 Docker 빌드 시작 (torch+OCR이라 **처음 빌드 10~20분** 걸림). 완료되면 `https://<HF아이디>-receiptly.hf.space` 로 접속.

---

## 배포 후 계속 개발하기

- 코드 수정 → `git push origin main` (GitHub) **+** `git push hf main` (배포)
- HF가 push 감지해서 자동 재빌드 → 몇 분 후 반영
- **데이터는 Atlas에 있어서 재배포해도 유지됨**
- API 키/DB주소는 `.env`가 아니라 **HF Secrets**에서 관리 (한 번 설정)

## 주의
- `.env`는 커밋 안 되므로(gitignore) HF엔 안 올라감 → 반드시 **HF Secrets**에 넣어야 함
- HF 무료 Space는 오래 미사용 시 잠들었다가 접속 시 깨어남(첫 접속 느림)
- 폰에서 `https://...hf.space` 접속 → **홈 화면에 추가**하면 PWA 앱처럼 설치됨 (HTTPS라 정상 동작)
