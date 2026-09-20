# 논곡중나무위키 v2.0

Flask + PostgreSQL 기반 학교 위키입니다.

## 배포
- Render Web Service
- PostgreSQL
- GitHub

Render Build Command: `pip install -r requirements.txt`
Start Command: `gunicorn app:app`

필수 환경변수:
- DATABASE_URL
- SECRET_KEY
- ADMIN_USERNAME
- ADMIN_PASSWORD

학교 구성원이 사용하는 공개 서비스이므로 전화번호, 주소, 계정정보, 사적인 내용, 동의 없는 사진 등 개인정보 게시를 금지하는 운영 규칙을 적용하세요.

온라인에서는 기본 관리자 비밀번호를 사용하지 마세요.
