"""自动写作API"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import verify_project_access
from app.database import get_db
from app.models.background_task import BackgroundTask
from app.models.project import Project
from app.schemas.auto_writing import (
    AutoWritingStartRequest,
    AutoWritingTaskDetailResponse,
    AutoWritingTaskResponse,
)
from app.services.auto_writing_service import run_auto_writing_background
from app.services.background_task_service import background_task_service

router = APIRouter(prefix="/auto-writing", tags=["自动写作"])


@router.post("/start", response_model=AutoWritingTaskResponse, summary="启动自动写作")
async def start_auto_writing_task(
    payload: AutoWritingStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """创建自动写作后台任务并加入当前用户队列。"""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    if payload.mode == "existing_project":
        project = await verify_project_access(payload.project_id, user_id, db)
    else:
        project = Project(
            user_id=user_id,
            title=payload.title,
            description=payload.description,
            theme=payload.theme,
            genre=payload.genre,
            target_words=payload.target_total_words,
            status="writing",
            wizard_status="completed",
            wizard_step=4,
            outline_mode=payload.outline_mode,
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)

    active_result = await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.user_id == user_id,
            BackgroundTask.project_id == project.id,
            BackgroundTask.task_type == "auto_writing",
            BackgroundTask.status.in_(["pending", "running", "paused"]),
        )
    )
    active_task = active_result.scalar_one_or_none()
    if active_task:
        raise HTTPException(status_code=400, detail="该项目已有自动写作任务正在运行")

    task_input = payload.model_dump()
    task_input["project_id"] = project.id
    task = await background_task_service.create_task(
        user_id=user_id,
        project_id=project.id,
        task_type="auto_writing",
        task_input=task_input,
        db=db,
    )
    await background_task_service.spawn_background_task(
        task.id,
        user_id,
        run_auto_writing_background,
    )

    return AutoWritingTaskResponse(
        task_id=task.id,
        project_id=project.id,
        status=task.status,
        message="自动写作任务已创建",
    )


@router.get("/{task_id}", response_model=AutoWritingTaskDetailResponse, summary="获取自动写作任务状态")
async def get_auto_writing_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    task = await _get_auto_writing_task_or_404(task_id, request, db)
    return _task_to_response(task)


@router.post("/{task_id}/pause", response_model=AutoWritingTaskDetailResponse, summary="暂停自动写作")
async def pause_auto_writing_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    task = await _get_auto_writing_task_or_404(task_id, request, db)
    if task.status not in ("pending", "running"):
        raise HTTPException(status_code=400, detail="只能暂停等待中或运行中的任务")

    task.status = "paused"
    task.status_message = "任务已暂停"
    task.progress_details = {"stage": "paused", "message": "任务已暂停"}
    task.updated_at = datetime.now()
    await db.commit()
    await db.refresh(task)
    return _task_to_response(task)


@router.post("/{task_id}/resume", response_model=AutoWritingTaskDetailResponse, summary="恢复自动写作")
async def resume_auto_writing_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    task = await _get_auto_writing_task_or_404(task_id, request, db)
    if task.status != "paused":
        raise HTTPException(status_code=400, detail="只能恢复已暂停的自动写作任务")

    user_id = getattr(request.state, "user_id", None)
    task.status = "pending"
    task.cancel_requested = False
    task.status_message = "任务已恢复，等待执行..."
    task.progress_details = {"stage": "queued", "message": "任务已恢复，等待执行..."}
    task.completed_at = None
    task.updated_at = datetime.now()
    await db.commit()
    await db.refresh(task)
    await background_task_service.spawn_background_task(
        task.id,
        user_id,
        run_auto_writing_background,
    )
    await db.refresh(task)
    return _task_to_response(task)


@router.post("/{task_id}/cancel", response_model=AutoWritingTaskDetailResponse, summary="取消自动写作")
async def cancel_auto_writing_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    task = await _get_auto_writing_task_or_404(task_id, request, db)
    if task.status not in ("pending", "running", "paused"):
        raise HTTPException(status_code=400, detail="无法取消已结束的任务")

    task.cancel_requested = True
    task.status = "cancelled"
    task.status_message = "任务已取消"
    task.completed_at = datetime.now()
    task.updated_at = datetime.now()
    await db.commit()
    await db.refresh(task)
    return _task_to_response(task)


async def _get_auto_writing_task_or_404(
    task_id: str,
    request: Request,
    db: AsyncSession,
) -> BackgroundTask:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.id == task_id,
            BackgroundTask.user_id == user_id,
            BackgroundTask.task_type == "auto_writing",
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="自动写作任务不存在")
    return task


def _task_to_response(task: BackgroundTask) -> AutoWritingTaskDetailResponse:
    return AutoWritingTaskDetailResponse(
        id=task.id,
        task_id=task.id,
        task_type=task.task_type,
        project_id=task.project_id,
        status=task.status,
        progress=task.progress or 0,
        status_message=task.status_message,
        progress_details=task.progress_details,
        error_message=task.error_message,
        task_input=task.task_input,
        task_result=task.task_result,
        retry_count=task.retry_count or 0,
        cancel_requested=bool(task.cancel_requested),
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        updated_at=task.updated_at,
    )
