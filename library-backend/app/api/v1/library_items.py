from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status, Query, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import (
    get_db, get_current_user, get_current_active_user, get_current_user_optional,
    common_parameters, CommonQueryParams
)
from app.crud.library_item import library_item_crud
from app.crud.user import user_crud
from app.schemas.library_item import (
    LibraryItemCreate, LibraryItemUpdate, LibraryItemResponse,
    ItemType, VisibilityType, PresignedUrlRequest, PresignedUrlResponse
)
from app.schemas.common import SuccessResponse, ErrorResponse, PaginatedResponse, PaginationInfo
from app.schemas.user import UserCreate
from app.models.user import User
from app.core.config import settings
from app.services.s3_service import s3_service
import logging
import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

router = APIRouter()


def verify_internal_api_key(x_internal_api_key: Optional[str] = Header(None)) -> bool:
    """내부 서비스 API 키 검증"""
    if x_internal_api_key and x_internal_api_key == settings.INTERNAL_API_KEY:
        return True
    return False


async def resolve_current_user(db: AsyncSession, current_user: Optional[User]):
    """현재 사용자 정보 반환 (user_id, nickname)"""
    if current_user:
        return current_user.user_id, current_user.nickname or current_user.user_id
    if not settings.DEBUG:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    test_user = await user_crud.get_by_user_id(db, user_id="test_user")
    if not test_user:
        test_user = await user_crud.create_user(
            db, user_in=UserCreate(user_id="test_user", nickname="테스트유저", email="test@test.com")
        )
    return test_user.user_id, test_user.nickname


@router.post(
    "/",
    response_model=SuccessResponse[LibraryItemResponse],
    status_code=status.HTTP_201_CREATED,
    summary="라이브러리 아이템 생성",
    description="새로운 라이브러리 아이템을 생성합니다. S3 업로드 완료 후 메타데이터를 저장할 때 사용합니다."
)
async def create_library_item(
    *,
    db: AsyncSession = Depends(get_db),
    item_in: LibraryItemCreate,
    current_user: Optional[User] = Depends(get_current_user_optional)
) -> SuccessResponse[LibraryItemResponse]:
    """
    라이브러리 아이템 생성 API
    - S3 업로드 완료 후 메타데이터 저장
    - 동영상인 경우 프리뷰/썸네일 생성 Step Functions 자동 트리거
    """
    try:
        user_id, username = await resolve_current_user(db, current_user)

        item = await library_item_crud.create_item(
            db, user_id=user_id, item_in=item_in
        )
        
        logger.info(f"새 라이브러리 아이템 생성: {item.name} (사용자: {username})")
        
        execution_arn = None
        if item_in.type == ItemType.video or (item_in.mime_type and item_in.mime_type.startswith('video/')):
            execution_arn = await s3_service.trigger_video_preview_generation(
                s3_key=item_in.s3_key,
                item_id=str(item.id)
            )
            if execution_arn:
                logger.info(f"동영상 프리뷰 생성 Step Functions 시작: {execution_arn}")
            else:
                logger.warning(f"동영상 프리뷰 생성 Step Functions 트리거 실패")
        
        return SuccessResponse(
            data=LibraryItemResponse.from_orm(item),
            message="라이브러리 아이템이 성공적으로 생성되었습니다"
        )
        
    except Exception as e:
        logger.error(f"라이브러리 아이템 생성 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라이브러리 아이템 생성 중 오류가 발생했습니다"
        )


@router.get(
    "/",
    response_model=PaginatedResponse[LibraryItemResponse],
    summary="내 라이브러리 아이템 목록 조회",
    description="현재 사용자의 라이브러리 아이템 목록을 조회합니다."
)
async def get_my_library_items(
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    commons: CommonQueryParams = Depends(common_parameters),
    item_type: Optional[ItemType] = Query(None, description="아이템 타입 필터"),
    search: Optional[str] = Query(None, description="검색 키워드"),
    include_deleted: bool = Query(False, description="삭제된 아이템 포함 여부")
) -> PaginatedResponse[LibraryItemResponse]:
    """
    내 라이브러리 아이템 목록 조회 API
    - S3에서 파일이 삭제된 경우 자동으로 soft delete 처리
    """
    try:
        user_id, _ = await resolve_current_user(db, current_user)

        if search:
            items = await library_item_crud.search_items(
                db, user_id=user_id, query=search,
                skip=commons.skip, limit=commons.limit
            )
            total = len(items)
        elif item_type:
            items = await library_item_crud.get_by_type(
                db, user_id=user_id, item_type=item_type,
                skip=commons.skip, limit=commons.limit
            )
            total = await library_item_crud.count_user_items(
                db, user_id=user_id, item_type=item_type
            )
        else:
            items = await library_item_crud.get_by_user(
                db, user_id=user_id,
                skip=commons.skip, limit=commons.limit,
                include_deleted=True
            )
            total = await library_item_crud.count_user_items(
                db, user_id=user_id, include_deleted=False
            )
        
        valid_items = []
        deleted_count = 0
        restored_count = 0
        
        for item in items:
            s3_exists = s3_service.file_exists(item.s3_key)
            
            if s3_exists:
                if item.deleted_at is not None:
                    logger.info(f"S3 파일 복구 감지, 자동 복원: {item.s3_key} (아이템: {item.name})")
                    await library_item_crud.restore(db, id=str(item.id))
                    restored_count += 1
                    item = await library_item_crud.get(db, id=str(item.id))
                
                if include_deleted or item.deleted_at is None:
                    valid_items.append(item)
            else:
                if item.deleted_at is None:
                    logger.warning(f"S3 파일 없음, 자동 soft delete: {item.s3_key} (아이템: {item.name})")
                    await library_item_crud.soft_delete(db, id=str(item.id))
                    deleted_count += 1
        
        if deleted_count > 0:
            logger.info(f"S3에서 삭제된 파일 {deleted_count}개 자동 정리 완료")
        
        if restored_count > 0:
            logger.info(f"S3에서 복구된 파일 {restored_count}개 자동 복원 완료")
        
        total = await library_item_crud.count_user_items(
            db, user_id=user_id, include_deleted=include_deleted
        )
        
        pages = max(1, (total + commons.limit - 1) // commons.limit)
        current_page = (commons.skip // commons.limit) + 1
        
        pagination_info = PaginationInfo(
            page=current_page,
            size=commons.limit,
            total=total,
            pages=pages,
            has_next=current_page < pages,
            has_prev=current_page > 1
        )
        
        response_items = [LibraryItemResponse.from_orm(item) for item in valid_items]
        
        return PaginatedResponse(
            data=response_items,
            pagination=pagination_info,
            message="라이브러리 아이템 목록 조회 성공"
        )
        
    except Exception as e:
        logger.error(f"라이브러리 아이템 목록 조회 중 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라이브러리 아이템 목록 조회 중 오류가 발생했습니다"
        )


@router.get(
    "/public",
    response_model=PaginatedResponse[LibraryItemResponse],
    summary="공개 라이브러리 아이템 목록 조회",
    description="공개로 설정된 라이브러리 아이템 목록을 조회합니다."
)
async def get_public_library_items(
    db: AsyncSession = Depends(get_db),
    commons: CommonQueryParams = Depends(common_parameters),
    item_type: Optional[ItemType] = Query(None, description="아이템 타입 필터")
) -> PaginatedResponse[LibraryItemResponse]:
    """
    공개 라이브러리 아이템 목록 조회 API
    """
    try:
        items = await library_item_crud.get_public_items(
            db, skip=commons.skip, limit=commons.limit, item_type=item_type
        )
        
        total = len(items) if len(items) < commons.limit else commons.skip + len(items) + 1
        
        pages = (total + commons.limit - 1) // commons.limit
        current_page = (commons.skip // commons.limit) + 1
        
        pagination_info = PaginationInfo(
            page=current_page,
            size=commons.limit,
            total=total,
            pages=pages,
            has_next=len(items) == commons.limit,
            has_prev=current_page > 1
        )
        
        return PaginatedResponse(
            data=[LibraryItemResponse.from_orm(item) for item in items],
            pagination=pagination_info,
            message="공개 라이브러리 아이템 목록 조회 성공"
        )
        
    except Exception as e:
        logger.error(f"공개 라이브러리 아이템 목록 조회 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="공개 라이브러리 아이템 목록 조회 중 오류가 발생했습니다"
        )


@router.get(
    "/url-by-key",
    summary="S3 Key로 파일 URL 조회",
    description="S3 Key를 받아서 파일 다운로드 URL을 반환합니다."
)
async def get_url_by_s3_key(
    s3_key: str,
    expires_in: int = Query(3600, description="URL 만료 시간 (초)")
):
    """
    S3 Key를 받아서 파일 URL 반환 API
    """
    try:
        logger.info(f"S3 Key URL 생성 요청: {s3_key}")
        logger.info(f"만료 시간: {expires_in}초")
        
        download_url = await s3_service.generate_presigned_download_url(
            s3_key=s3_key,
            expires_in=expires_in
        )
        
        logger.info(f"S3 URL 생성 성공: {s3_key}")
        logger.info(f"생성된 URL: {download_url[:100]}...")
        
        return {
            "success": True,
            "data": {
                "file_url": download_url,
                "s3_key": s3_key,
                "expires_in": expires_in
            },
            "message": "파일 URL이 성공적으로 생성되었습니다"
        }
        
    except Exception as e:
        logger.error(f"S3 URL 생성 중 오류: {e}")
        logger.error(f"오류 타입: {type(e).__name__}")
        logger.error(f"오류 상세: {str(e)}")
        import traceback
        logger.error(f"스택 트레이스: {traceback.format_exc()}")
        
        return {
            "success": False,
            "message": f"파일 URL 생성 중 오류가 발생했습니다: {str(e)}"
        }


@router.get(
    "/{item_id}",
    response_model=SuccessResponse[LibraryItemResponse],
    summary="라이브러리 아이템 상세 조회",
    description="특정 라이브러리 아이템의 상세 정보를 조회합니다."
)
async def get_library_item(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
) -> SuccessResponse[LibraryItemResponse]:
    """
    라이브러리 아이템 상세 조회 API
    - 공개 아이템이거나 소유자인 경우 조회 가능
    """
    try:
        item = await library_item_crud.get(db, id=item_id)
        
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="라이브러리 아이템을 찾을 수 없습니다"
            )
        
        is_owner = current_user and str(item.user_id) == str(current_user.user_id)
        is_public = item.visibility == VisibilityType.public
        
        if not (is_owner or is_public):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="이 아이템에 대한 접근 권한이 없습니다"
            )
        
        return SuccessResponse(
            data=LibraryItemResponse.from_orm(item),
            message="라이브러리 아이템 조회 성공"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"라이브러리 아이템 조회 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라이브러리 아이템 조회 중 오류가 발생했습니다"
        )


@router.put(
    "/{item_id}",
    response_model=SuccessResponse[LibraryItemResponse],
    summary="라이브러리 아이템 수정",
    description="라이브러리 아이템 정보를 수정합니다. 소유자만 수정 가능합니다."
)
async def update_library_item(
    item_id: str,
    *,
    db: AsyncSession = Depends(get_db),
    item_in: LibraryItemUpdate,
    current_user: Optional[User] = Depends(get_current_user_optional),
    x_internal_api_key: Optional[str] = Header(None)
) -> SuccessResponse[LibraryItemResponse]:
    """
    라이브러리 아이템 수정 API
    - 내부 서비스(Lambda)에서 X-Internal-API-Key 헤더로 인증 가능
    """
    try:
        is_internal_service = verify_internal_api_key(x_internal_api_key)
        
        if is_internal_service:
            logger.info(f"내부 서비스에서 아이템 수정 요청: {item_id}")
            item = await library_item_crud.get(db, id=item_id)
            if not item:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="라이브러리 아이템을 찾을 수 없습니다"
                )
            
            if item_in.name is not None:
                item.name = item_in.name
            if item_in.visibility is not None:
                item.visibility = item_in.visibility
            
            await db.commit()
            await db.refresh(item)
            
            logger.info(f"내부 서비스 아이템 수정 완료: {item.name}")
            
            return SuccessResponse(
                data=LibraryItemResponse.from_orm(item),
                message="라이브러리 아이템이 성공적으로 수정되었습니다"
            )
        
        user_id, username = await resolve_current_user(db, current_user)

        updated_item = await library_item_crud.update_item(
            db, item_id=item_id, user_id=user_id, item_in=item_in
        )
        
        if not updated_item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="라이브러리 아이템을 찾을 수 없거나 수정 권한이 없습니다"
            )
        
        logger.info(f"라이브러리 아이템 수정: {updated_item.name} (사용자: {username})")
        
        return SuccessResponse(
            data=LibraryItemResponse.from_orm(updated_item),
            message="라이브러리 아이템이 성공적으로 수정되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"라이브러리 아이템 수정 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라이브러리 아이템 수정 중 오류가 발생했습니다"
        )


@router.delete(
    "/{item_id}",
    response_model=SuccessResponse[dict],
    summary="라이브러리 아이템 삭제",
    description="라이브러리 아이템을 삭제합니다. 소유자만 삭제 가능합니다."
)
async def delete_library_item(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    permanent: bool = Query(True, description="영구 삭제 여부 (기본값: 영구 삭제)")
) -> SuccessResponse[dict]:
    """
    라이브러리 아이템 삭제 API
    """
    try:
        user_id, username = await resolve_current_user(db, current_user)

        deleted_item = await library_item_crud.delete_item(
            db, item_id=item_id, user_id=user_id, 
            soft_delete=not permanent
        )
        
        if not deleted_item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="라이브러리 아이템을 찾을 수 없거나 삭제 권한이 없습니다"
            )
        
        delete_type = "영구 삭제" if permanent else "소프트 삭제"
        logger.info(f"라이브러리 아이템 {delete_type}: {deleted_item.name} (사용자: {username})")
        
        return SuccessResponse(
            data={"deleted": True, "permanent": permanent},
            message=f"라이브러리 아이템이 성공적으로 {'영구 삭제' if permanent else '삭제'}되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"라이브러리 아이템 삭제 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라이브러리 아이템 삭제 중 오류가 발생했습니다"
        )


@router.post(
    "/{item_id}/restore",
    response_model=SuccessResponse[LibraryItemResponse],
    summary="삭제된 라이브러리 아이템 복원",
    description="소프트 삭제된 라이브러리 아이템을 복원합니다."
)
async def restore_library_item(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
) -> SuccessResponse[LibraryItemResponse]:
    """
    삭제된 라이브러리 아이템 복원 API
    """
    try:
        user_id, username = await resolve_current_user(db, current_user)

        restored_item = await library_item_crud.restore_item(
            db, item_id=item_id, user_id=user_id
        )
        
        if not restored_item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="라이브러리 아이템을 찾을 수 없거나 복원 권한이 없습니다"
            )
        
        logger.info(f"라이브러리 아이템 복원: {restored_item.name} (사용자: {username})")
        
        return SuccessResponse(
            data=LibraryItemResponse.from_orm(restored_item),
            message="라이브러리 아이템이 성공적으로 복원되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"라이브러리 아이템 복원 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라이브러리 아이템 복원 중 오류가 발생했습니다"
        )


@router.get(
    "/stats/summary",
    response_model=SuccessResponse[dict],
    summary="내 라이브러리 통계 조회",
    description="현재 사용자의 라이브러리 통계를 조회합니다."
)
async def get_my_library_stats(
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
) -> SuccessResponse[dict]:
    """
    내 라이브러리 통계 조회 API
    """
    try:
        user_id, _ = await resolve_current_user(db, current_user)

        stats = await library_item_crud.get_user_stats(
            db, user_id=user_id
        )
        
        return SuccessResponse(
            data=stats,
            message="라이브러리 통계 조회 성공"
        )
        
    except Exception as e:
        logger.error(f"라이브러리 통계 조회 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라이브러리 통계 조회 중 오류가 발생했습니다"
        )


@router.post(
    "/upload/presigned-url",
    response_model=SuccessResponse[PresignedUrlResponse],
    summary="S3 업로드용 Presigned URL 생성",
    description="S3에 파일을 업로드하기 위한 Presigned URL을 생성합니다."
)
async def generate_presigned_url(
    *,
    request: PresignedUrlRequest,
    current_user: Optional[User] = Depends(get_current_user_optional)
) -> SuccessResponse[PresignedUrlResponse]:
    """
    S3 업로드용 Presigned URL 생성 API
    """
    try:
        from app.services.s3_service import s3_service
        from app.services.file_service import file_service
        
        valid, error_msg, file_info = file_service.validate_upload_request(
            filename=request.filename,
            content_type=request.content_type,
            file_size=request.file_size
        )
        
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )
        
        if not current_user:
            if not settings.DEBUG:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="인증이 필요합니다",
                )
            user_id = "test_user"
            nickname = "테스트유저"
        else:
            user_id = current_user.user_id
            nickname = current_user.nickname or current_user.user_id

        upload_info = await s3_service.generate_presigned_upload_url(
            filename=request.filename,
            content_type=request.content_type,
            user_id=user_id
        )
        
        logger.info(f"Presigned URL 생성: {request.filename} (사용자: {nickname})")
        
        return SuccessResponse(
            data=PresignedUrlResponse(
                upload_url=upload_info["upload_url"],
                s3_key=upload_info["s3_key"],
                expires_in=upload_info["expires_in"],
                fields=upload_info.get("fields", {}),
                file_info=file_info
            ),
            message="업로드 URL이 성공적으로 생성되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Presigned URL 생성 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="업로드 URL 생성 중 오류가 발생했습니다"
        )


@router.post(
    "/{item_id}/generate-title",
    response_model=SuccessResponse[dict],
    summary="AI 제목 생성",
    description="동영상 내용을 분석하여 AI로 제목을 생성합니다."
)
async def generate_title_with_ai(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
) -> SuccessResponse[dict]:
    """
    AI 제목 생성 API
    - Step Functions (Standard)를 호출하여 Rekognition Video + Bedrock으로 제목 생성
    """
    try:
        user_id, username = await resolve_current_user(db, current_user)
        
        item = await library_item_crud.get(db, id=item_id)
        
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="라이브러리 아이템을 찾을 수 없습니다"
            )
        
        if str(item.user_id) != str(user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="이 아이템에 대한 권한이 없습니다"
            )
        
        if item.type.value != 'video':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="동영상 아이템만 AI 제목 생성이 가능합니다"
            )
        
        sfn_client = boto3.client('stepfunctions', region_name=settings.AWS_REGION)
        
        import json
        try:
            running_executions = sfn_client.list_executions(
                stateMachineArn=settings.AI_TITLE_GENERATOR_STATE_MACHINE_ARN,
                statusFilter='RUNNING',
                maxResults=100
            )
            
            for execution in running_executions.get('executions', []):
                exec_detail = sfn_client.describe_execution(
                    executionArn=execution['executionArn']
                )
                exec_input = json.loads(exec_detail.get('input', '{}'))
                
                if exec_input.get('item_id') == str(item.id):
                    logger.warning(f"이미 실행 중인 AI 제목 생성이 있습니다: {execution['executionArn']}")
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="이미 AI 제목 생성이 진행 중입니다. 잠시 후 다시 시도해주세요."
                    )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"중복 실행 체크 중 오류 (무시하고 진행): {e}")
        
        response = sfn_client.start_execution(
            stateMachineArn=settings.AI_TITLE_GENERATOR_STATE_MACHINE_ARN,
            input=json.dumps({
                'bucket': settings.S3_BUCKET_NAME,
                'key': item.s3_key,
                'item_id': str(item.id)
            })
        )
        
        execution_arn = response['executionArn']
        logger.info(f"AI 제목 생성 Step Functions 시작: {execution_arn} (아이템: {item_id}, 사용자: {username})")
        
        return SuccessResponse(
            data={
                'execution_arn': execution_arn,
                'item_id': item_id,
                'status': 'STARTED',
                'message': 'AI 제목 생성이 시작되었습니다. 동영상 분석에 1-2분 정도 소요됩니다.'
            },
            message="AI 제목 생성이 시작되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI 제목 생성 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI 제목 생성 중 오류가 발생했습니다"
        )


@router.get(
    "/file/{s3_key:path}",
    summary="S3 파일 프록시",
    description="S3 파일을 백엔드를 통해 프록시합니다. IRSA 인증 문제를 우회합니다."
)
async def proxy_s3_file(s3_key: str):
    """
    S3 파일 프록시 API
    """
    try:
        logger.info(f"S3 파일 프록시 요청: {s3_key}")
        
        s3_client = boto3.client(
            's3',
            region_name=settings.S3_REGION,
            config=Config(signature_version='s3v4')
        )
        
        response = s3_client.get_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=s3_key
        )
        
        content_type = response.get('ContentType', 'application/octet-stream')
        
        logger.info(f"S3 파일 프록시 성공: {s3_key} ({content_type})")
        
        return StreamingResponse(
            response['Body'].iter_chunks(),
            media_type=content_type,
            headers={
                "Content-Disposition": f"inline; filename={s3_key.split('/')[-1]}",
                "Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*"
            }
        )
        
    except s3_client.exceptions.NoSuchKey:
        logger.error(f"S3 파일 없음: {s3_key}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="파일을 찾을 수 없습니다"
        )
    except Exception as e:
        logger.error(f"S3 파일 프록시 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"파일 조회 중 오류가 발생했습니다: {str(e)}"
        )
