# FastAPI 서버 실행 스크립트

"""
FastAPI 개발 서버 실행 스크립트
- 개발 환경에서 서버를 쉽게 시작할 수 있도록 도움
- 환경 변수 로드 및 설정 확인
"""

import uvicorn
import os
import sys
from pathlib import Path

# 프로젝트 루트를 Python 경로에 추가
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 환경 변수 파일 확인
env_file = project_root / ".env"
if not env_file.exists():
    print("⚠️  .env 파일이 없습니다. .env.example을 참고하여 .env 파일을 생성하세요.")
    print("📝 최소 필요한 환경 변수:")
    print("   - DATABASE_URL")
    print("   - JWT_SECRET_KEY")
    print("   - AWS 관련 설정 (선택사항)")
    print()

try:
    # 설정 로드 테스트
    from app.core.config import settings
    print(f"🔧 설정 로드 완료")
    print(f"📊 데이터베이스: {settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}")
    print(f"🌐 서버: {settings.HOST}:{settings.PORT}")
    print(f"🔐 디버그 모드: {settings.DEBUG}")
    print()
    
    # 서버 시작
    print("🚀 FastAPI 서버를 시작합니다...")
    print(f"📖 API 문서: http://{settings.HOST}:{settings.PORT}/api/v1/docs")
    print(f"🔍 ReDoc: http://{settings.HOST}:{settings.PORT}/api/v1/redoc")
    print(f"❤️  헬스체크: http://{settings.HOST}:{settings.PORT}/health")
    print()
    
    if __name__ == "__main__":
        uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info" if settings.DEBUG else "warning",
        access_log=settings.DEBUG
        )
    
except ImportError as e:
    print(f"❌ 모듈 import 오류: {e}")
    print("💡 다음 명령어로 의존성을 설치하세요:")
    print("   pip install -r requirements.txt")
    sys.exit(1)
    
except Exception as e:
    print(f"❌ 서버 시작 중 오류: {e}")
    sys.exit(1)
