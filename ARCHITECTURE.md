# Receiptly 아키텍처

> 이미지 버전: [architecture.svg](architecture.svg) (브라우저로 열면 그림으로 보여요)

## 현재 (로컬)

```mermaid
flowchart TB
    subgraph device["📱 사용자 기기 (브라우저)"]
        UI["단일 HTML + JS<br/>달력 · 분석 · 위시리스트 · 챗봇"]
    end

    subgraph server["⚙️ FastAPI 서버 (main.py · uvicorn :8000)"]
        routes["엔드포인트<br/>/auth · /add/expense · /api/expenses<br/>/api/analysis · /api/wishlist · /chat"]
        ocr["🔍 EasyOCR<br/>영수증 인식"]
        parse["🧠 파싱·분류<br/>정규식 + 과거학습"]
        html["HTML/JS 페이지 내장(문자열)"]
    end

    subgraph db["🗄️ MongoDB (Docker :27017, 볼륨 mongo_data)"]
        users["users"]
        sessions["sessions"]
        expenses["expenses"]
        wishlist["wishlist"]
    end

    subgraph ext["🌐 외부 API (.env 키)"]
        odsay["ODsay<br/>지하철 요금"]
        naver["네이버 쇼핑<br/>제품 검색"]
    end

    UI <-->|"HTTP · 세션 쿠키"| routes
    routes <-->|"motor (async)"| db
    routes -->|"HTTPS"| odsay
    routes -->|"HTTPS"| naver
    routes --> ocr
    routes --> parse
```

**핵심**: UI·API·OCR이 전부 한 FastAPI 프로세스(`main.py`)에 있고, DB는 로컬 Docker MongoDB. 외부 API 키는 서버의 `.env`에만 있음(브라우저 미노출). **전부 localhost라 이 컴퓨터에서만 접근 가능**, 데이터는 Docker 볼륨에 저장.

## 앱/배포로 가려면 (목표 아키텍처)

```mermaid
flowchart TB
    app["📱 앱/PWA<br/>(React Native 또는 PWA)"]
    api["☁️ FastAPI (배포: Railway/Render 등)<br/>HTTPS · 토큰(JWT) 인증"]
    atlas["🗄️ MongoDB Atlas (클라우드 DB)"]
    ext2["🌐 ODsay · 네이버 API"]

    app <-->|"HTTPS · Bearer 토큰"| api
    api <-->|"TLS"| atlas
    api --> ext2
```

**바뀌는 점**: ① DB를 Atlas(클라우드)로, ② 백엔드를 공개 서버에 배포(HTTPS), ③ 네이티브 앱이면 쿠키 대신 토큰 인증, ④ 배포 전 보안(비밀번호 bcrypt·XSS·세션 만료) 정리. UI를 API에서 분리(현재 `/api/*`가 이미 있어 기반은 있음).
