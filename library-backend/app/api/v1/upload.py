from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from app.api.deps import get_db, get_current_active_user, get_current_user_optional
from app.core.config import settings
from app.services.s3_service import s3_service
from app.services.file_service import file_service
from app.schemas.library_item import PresignedUrlRequest, PresignedUrlResponse, LibraryItemCreate
from app.schemas.common import SuccessResponse
from app.models.user import User
from app.crud.library_item import library_item_crud
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/presigned-url",
    response_model=SuccessResponse[PresignedUrlResponse],
    summary="S3 업로드용 Presigned URL 생성 (실제 S3)",
    description="실제 AWS S3에 파일을 업로드하기 위한 Presigned URL을 생성합니다."
)
async def generate_real_presigned_url(
    *,
    request: PresignedUrlRequest,
    current_user: Optional[User] = Depends(get_current_user_optional)
) -> SuccessResponse[PresignedUrlResponse]:
    """
    실제 S3 Presigned URL 생성 API
    - AWS S3 클라이언트를 사용하여 실제 업로드 URL 생성
    """
    try:
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
            username = "test_user"
        else:
            user_id = current_user.user_id
            username = current_user.nickname or current_user.user_id

        upload_info = await s3_service.generate_presigned_upload_url(
            filename=request.filename,
            content_type=request.content_type,
            user_id=user_id
        )
        
        logger.info(f"실제 S3 Presigned URL 생성: {request.filename} (사용자: {username})")
        
        return SuccessResponse(
            data=PresignedUrlResponse(
                upload_url=upload_info["upload_url"],
                s3_key=upload_info["s3_key"],
                expires_in=upload_info["expires_in"],
                fields=upload_info.get("fields", {}),
                file_info=file_info
            ),
            message="실제 S3 업로드 URL이 성공적으로 생성되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"S3 Presigned URL 생성 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="업로드 URL 생성 중 오류가 발생했습니다"
        )


@router.get(
    "/download/{item_id}",
    response_model=SuccessResponse[Dict[str, str]],
    summary="S3 파일 다운로드 URL 생성",
    description="S3에 저장된 파일의 다운로드 URL을 생성합니다."
)
async def generate_download_url(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
) -> SuccessResponse[Dict[str, str]]:
    """
    S3 파일 다운로드 URL 생성 API
    """
    try:
        from app.crud.library_item import library_item_crud
        
        item = await library_item_crud.get(db, id=item_id)
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="파일을 찾을 수 없습니다"
            )
        
        is_owner = str(item.user_profile_id) == str(current_user.id)
        is_public = item.visibility == "public"
        
        if not (is_owner or is_public):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="이 파일에 대한 접근 권한이 없습니다"
            )
        
        download_url = await s3_service.generate_presigned_download_url(
            s3_key=item.s3_key,
            expires_in=3600
        )
        
        thumbnail_url = None
        if item.s3_thumbnail_key:
            thumbnail_url = await s3_service.generate_presigned_download_url(
                s3_key=item.s3_thumbnail_key,
                expires_in=3600
            )
        
        logger.info(f"다운로드 URL 생성: {item.name} (사용자: {current_user.user_id})")
        
        result = {
            "download_url": download_url,
            "filename": item.original_filename,
            "file_size": str(item.file_size)
        }
        
        if thumbnail_url:
            result["thumbnail_url"] = thumbnail_url
        
        return SuccessResponse(
            data=result,
            message="다운로드 URL이 성공적으로 생성되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"다운로드 URL 생성 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="다운로드 URL 생성 중 오류가 발생했습니다"
        )


@router.post(
    "/upload-and-get-url",
    response_model=SuccessResponse[Dict[str, str]],
    summary="파일 업로드 후 S3 Key 반환",
    description="파일을 업로드하고 S3 Key를 반환합니다."
)
async def upload_file_and_get_url(
    file: UploadFile = File(...),
    name: str = Form(...),
    visibility: str = Form("private"),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
) -> SuccessResponse[Dict[str, str]]:
    """
    파일 업로드 후 S3 Key 즉시 반환 API
    """
    try:
        if not current_user:
            if not settings.DEBUG:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="인증이 필요합니다"
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="사용자 처리 오류"
            )
        
        user_id = current_user.user_id
        logger.info(f"업로드 사용자: {current_user.user_id} (ID: {user_id})")

        valid, error_msg, file_info = file_service.validate_upload_request(
            filename=file.filename,
            content_type=file.content_type,
            file_size=file.size
        )
        
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )

        s3_key = s3_service.generate_s3_key(file.filename, user_id)
        
        file_content = await file.read()
        
        upload_success = await s3_service.upload_file_content(
            s3_key=s3_key,
            file_content=file_content,
            content_type=file.content_type,
            metadata={
                "user-id": user_id,
                "original-filename": file.filename,
                "upload-timestamp": str(int(datetime.utcnow().timestamp()))
            }
        )
        
        if not upload_success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="S3 파일 업로드 실패"
            )
        
        item_data = LibraryItemCreate(
            name=name,
            type=file_info["item_type"],
            visibility=visibility,
            mime_type=file.content_type,
            s3_key=s3_key,
            file_size=file.size,
            original_filename=file.filename
        )
        
        item = await library_item_crud.create_item(
            db, user_id=user_id, item_in=item_data
        )

        execution_arn = None
        if s3_service.is_video_file(file.content_type):
            execution_arn = await s3_service.trigger_video_preview_generation(
                s3_key=s3_key,
                item_id=str(item.id)
            )
            if execution_arn:
                logger.info(f"프리뷰 생성 시작: {execution_arn}")

        logger.info(f"파일 업로드 완료: {file.filename} -> {s3_key}")
        
        response_data = {
            "item_id": str(item.id),
            "s3_key": s3_key,
            "filename": file.filename,
            "file_size": str(file.size)
        }
        
        if execution_arn:
            response_data["preview_generation_started"] = True
            response_data["execution_arn"] = execution_arn
        
        return SuccessResponse(
            data=response_data,
            message="파일 업로드 및 S3 Key 생성 완료"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"파일 업로드 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="파일 업로드 중 오류가 발생했습니다"
        )


@router.post(
    "/preview-callback",
    response_model=SuccessResponse[Dict[str, str]],
    summary="프리뷰 생성 완료 콜백",
    description="Step Functions에서 프리뷰 생성 완료 후 호출하는 콜백 API"
)
async def preview_generation_callback(
    item_id: str = Form(...),
    preview_key: str = Form(...),
    thumbnail_key: Optional[str] = Form(None),
    subtitle_key: Optional[str] = Form(None),
    transcribe_key: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db)
) -> SuccessResponse[Dict[str, str]]:
    """
    프리뷰 생성 완료 콜백 API
    """
    try:
        item = await library_item_crud.get(db, id=item_id)
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="아이템을 찾을 수 없습니다"
            )
        
        item.s3_preview_key = preview_key
        
        if thumbnail_key:
            item.s3_thumbnail_key = thumbnail_key
            logger.info(f"썸네일 키 업데이트: {item_id} -> {thumbnail_key}")
        
        if subtitle_key:
            item.s3_subtitle_key = subtitle_key
            logger.info(f"자막 키 업데이트: {item_id} -> {subtitle_key}")
        
        if transcribe_key:
            item.s3_transcribe_key = transcribe_key
            logger.info(f"Transcribe 키 업데이트: {item_id} -> {transcribe_key}")
        
        await db.commit()
        await db.refresh(item)
        
        logger.info(f"프리뷰 키 업데이트 완료: {item_id} -> {preview_key}")
        
        result = {
            "item_id": item_id,
            "preview_key": preview_key,
            "status": "updated"
        }
        
        if thumbnail_key:
            result["thumbnail_key"] = thumbnail_key
        
        if subtitle_key:
            result["subtitle_key"] = subtitle_key
        
        if transcribe_key:
            result["transcribe_key"] = transcribe_key
        
        return SuccessResponse(
            data=result,
            message="프리뷰 키가 성공적으로 업데이트되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"프리뷰 콜백 처리 중 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="프리뷰 콜백 처리 중 오류가 발생했습니다"
        )
