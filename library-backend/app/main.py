from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from app.core.config import settings
from app.api.v1.api import api_router
from app.database.base import test_connection, close_db_connections
from app.schemas.common import HealthCheckResponse, ErrorResponse
from datetime import datetime
import logging
import sys

from app.core.tracing import setup_tracing
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from sqlalchemy import create_engine
from app.database.models_config import Base
from app.models.user import User
from app.models.library_item import LibraryItem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


async def create_tables():
    """SQLAlchemy로 테이블 자동 생성"""
    try:
        logger.info("데이터베이스 테이블 생성 중...")
        engine = create_engine(settings.database_url_sync)
        Base.metadata.create_all(bind=engine)
        logger.info("데이터베이스 테이블 생성 완료")
        for table_name in Base.metadata.tables.keys():
            logger.info(f"  - {table_name}")
    except Exception as e:
        logger.error(f"테이블 생성 중 오류: {e}")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    애플리케이션 생명주기 관리
    """
    logger.info("FastAPI 애플리케이션 시작")
    
    setup_tracing("library-backend")
    HTTPXClientInstrumentor().instrument()
    logger.info("OpenTelemetry Instrumentation 완료")
    
    db_connected = await test_connection()
    if not db_connected:
        logger.error("데이터베이스 연결 실패 - 애플리케이션을 종료합니다")
        sys.exit(1)
    
    await create_tables()
    logger.info("애플리케이션 초기화 완료")
    
    yield
    
    logger.info("FastAPI 애플리케이션 종료")
    await close_db_connections()
    logger.info("리소스 정리 완료")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=settings.DESCRIPTION,
    openapi_url="/library/openapi.json",
    docs_url="/library/docs",
    redoc_url="/library/redoc",
    lifespan=lifespan
)

import os
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            success=False,
            message=exc.detail,
            error_code=f"HTTP_{exc.status_code}"
        ).dict()
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc: Exception):
    logger.error(f"예상치 못한 오류: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            success=False,
            message="내부 서버 오류가 발생했습니다",
            error_code="INTERNAL_SERVER_ERROR"
        ).dict()
    )


@app.get("/", include_in_schema=False)
async def root():
    return {
        "message": f"Welcome to {settings.PROJECT_NAME}",
        "version": settings.VERSION,
        "docs": "/library/docs",
        "redoc": "/library/redoc"
    }


@app.get("/library", include_in_schema=False)
async def library_root():
    return {
        "message": f"Welcome to {settings.PROJECT_NAME}",
        "version": settings.VERSION,
        "docs": "/library/docs",
        "redoc": "/library/redoc"
    }


@app.get(
    "/library/health",
    response_model=HealthCheckResponse,
    summary="헬스체크",
    description="애플리케이션 및 데이터베이스 상태를 확인합니다.",
    tags=["health"]
)
async def health_check():
    try:
        db_status = "connected" if await test_connection() else "disconnected"
        return HealthCheckResponse(
            status="healthy",
            timestamp=datetime.utcnow(),
            version=settings.VERSION,
            database=db_status
        )
    except Exception as e:
        logger.error(f"헬스체크 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="서비스가 일시적으로 사용할 수 없습니다"
        )


@app.get("/health", include_in_schema=False)
async def health_check_legacy():
    db_status = "connected" if await test_connection() else "disconnected"
    return HealthCheckResponse(
        status="healthy",
        timestamp=datetime.utcnow(),
        version=settings.VERSION,
        database=db_status
    )


app.include_router(
    api_router,
    prefix="/library"
)

FastAPIInstrumentor.instrument_app(app)


if __name__ == "__main__":
    import uvicorn
    logger.info("개발 모드에서 서버 시작")
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info" if settings.DEBUG else "warning"
    )