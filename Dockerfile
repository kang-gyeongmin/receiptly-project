# Hugging Face Spaces (Docker) 용 — FastAPI + EasyOCR
FROM python:3.12-slim

# OpenCV/torch 런타임 시스템 라이브러리
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# HF Spaces는 uid 1000 유저로 실행됨
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    EASYOCR_MODEL_DIR=/home/user/models
WORKDIR /home/user/app

# 의존성 먼저 설치 (레이어 캐시)
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# OCR 모델을 이미지에 미리 구워넣기 (런타임 재다운로드 방지)
RUN python -c "import ssl, certifi; ssl._create_default_https_context=lambda *a, **k: ssl.create_default_context(cafile=certifi.where()); import easyocr; easyocr.Reader(['ko','en'], model_storage_directory='/home/user/models')"

# 앱 소스 복사
COPY --chown=user . .

# HF Spaces 기본 포트
EXPOSE 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
