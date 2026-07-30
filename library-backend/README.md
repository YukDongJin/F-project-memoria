# Library Backend (추억 보관함 API)

FastAPI 기반 파일 및 메모리 관리 백엔드 서비스입니다. S3 Presigned URL을 통한 안전한 파일 업로드/다운로드, S3 실시간 동기화, AI 제목 생성 및 라이브러리 아이템 CRUD를 제공합니다.

## 기술 스택

- **FastAPI** + **SQLAlchemy** (asyncpg)
- **PostgreSQL** (RDS) - 메타데이터 저장
- **AWS S3** - 파일 및 미디어 저장 (Presigned URL & Proxy 지원)
- **AWS Secrets Manager** - DB 자격증명 동적 관리
- **AWS Step Functions & Bedrock** - 동영상 썸네일/자막 및 AI 제목 자동 생성
- **ElastiCache (Redis)** - 캐싱 계층
- **OpenTelemetry + Jaeger** - 마이크로서비스 분산 트레이싱
- **Cognito JWT** - 사용자 인증 및 보안
- **Alembic** - DB 마이그레이션

## 프로젝트 구조

```
library-backend/
├── app/
│   ├── main.py              # FastAPI 앱 (lifespan, CORS, 예외처리, OTel)
│   ├── api/
│   │   ├── deps.py          # Cognito JWT 및 공통 파라미터 의존성
│   │   └── v1/
│   │       ├── library_items.py  # 라이브러리 CRUD, AI 제목 생성, 프록시 API
│   │       ├── upload.py         # S3 Presigned URL 및 콜백 처리
│   │       └── users.py         # 사용자 관리 및 프로필 API
│   ├── core/
│   │   ├── config.py        # 설정 (Secrets Manager 연동)
│   │   └── tracing.py       # OpenTelemetry 초기화
│   ├── database/            # DB 연결 및 비동기 세션 관리
│   ├── models/              # SQLAlchemy 모델 (User, LibraryItem)
│   ├── schemas/             # Pydantic 스키마
│   └── services/
│       ├── file_service.py  # 파일 검증 로직
│       └── s3_service.py    # S3 Presigned URL & Step Functions 트리거
├── k8s/                     # EKS 배포용 Kubernetes 매니페스트
├── alembic/                 # DB 마이그레이션
├── Dockerfile
└── requirements.txt
```

## API 주요 엔드포인트

| Method | Endpoint | 설명 | JWT 인증 |
|--------|----------|------|------|
| GET | `/library/health` | ALB & K8s 헬스체크 | 불필요 (Public) |
| GET | `/library/library-items/` | 내 라이브러리 목록 조회 (S3 동기화) | 필요 (Cognito) |
| POST | `/library/library-items/` | 라이브러리 아이템 생성 | 필요 (Cognito) |
| GET | `/library/library-items/{id}` | 아이템 상세 조회 | 필요 (Cognito) |
| PUT | `/library/library-items/{id}` | 아이템 수정 (`X-Internal-API-Key` 지원) | 필요 (Cognito) |
| DELETE | `/library/library-items/{id}` | 아이템 삭제 (영구/소프트 삭제) | 필요 (Cognito) |
| POST | `/library/library-items/{id}/restore` | 소프트 삭제된 아이템 복원 | 필요 (Cognito) |
| GET | `/library/library-items/url-by-key` | S3 Key 기반 파일 URL 즉시 생성 | 불필요 (Public) |
| GET | `/library/library-items/file/{s3_key}` | S3 파일 프록시 스트리밍 | 불필요 (Public) |
| POST | `/library/library-items/{id}/generate-title` | AI 기반 동영상 제목 생성 (Step Functions) | 필요 (Cognito) |
| GET | `/library/library-items/stats/summary` | 내 라이브러리 사용 통계 조회 | 필요 (Cognito) |
| POST | `/library/upload/presigned-url` | S3 업로드 Presigned URL 발급 | 필요 (Cognito) |
| POST | `/library/upload/preview-callback` | Step Functions 완료 미디어 콜백 | 불필요 (Public) |

## 핵심 고도화 기능

1. **S3 자동 동기화 (Auto Soft-delete & Restore)**: S3 버킷 내 파일 존재 여부를 체크하여 파일 삭제 시 자동으로 DB soft-delete 처리하고, 파일 복구 시 자동 복원합니다.
2. **S3 파일 프록시 스트리밍**: IRSA Presigned URL CORS 제한을 우회하기 위해 백엔드를 거치는 직접 스트리밍 API를 지원합니다.
3. **내부 서비스 보안 인증**: `X-Internal-API-Key` 헤더를 통해 AWS Lambda나 비동기 워크플로우 서비스가 토큰 없이 안전하게 상태를 업데이트합니다.

## 실행 방법

```bash
# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정 (.env)
cp .env.example .env

# 서버 실행
python run_server.py

# API Swagger 문서: http://localhost:8000/library/docs
```
