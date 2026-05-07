"""自动写作后台编排服务"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_engine
from app.logger import get_logger
from app.models.analysis_task import AnalysisTask
from app.models.background_task import BackgroundTask
from app.models.chapter import Chapter
from app.models.memory import PlotAnalysis
from app.models.outline import Outline
from app.models.project import Project
from app.models.regeneration_task import RegenerationTask
from app.models.writing_style import WritingStyle
from app.schemas.regeneration import ChapterRegenerateRequest, PreserveElementsConfig
from app.services.background_task_service import TaskProgressTracker

logger = get_logger(__name__)


@dataclass(frozen=True)
class QualityGateConfig:
    overall_threshold: float
    coherence_threshold: float
    pacing_threshold: Optional[float] = None
    engagement_threshold: Optional[float] = None


@dataclass(frozen=True)
class QualityScores:
    overall: float
    coherence: float
    pacing: float
    engagement: float


@dataclass(frozen=True)
class QualityGateResult:
    passed: bool
    failing_scores: List[str]


def evaluate_quality_gate(
    scores: QualityScores,
    config: QualityGateConfig,
) -> QualityGateResult:
    """按配置评估章节质量门。"""
    checks = [
        ("overall", scores.overall, config.overall_threshold),
        ("coherence", scores.coherence, config.coherence_threshold),
    ]
    if config.pacing_threshold is not None:
        checks.append(("pacing", scores.pacing, config.pacing_threshold))
    if config.engagement_threshold is not None:
        checks.append(("engagement", scores.engagement, config.engagement_threshold))

    failing_scores = [
        name for name, score, threshold in checks
        if score < threshold
    ]
    return QualityGateResult(passed=not failing_scores, failing_scores=failing_scores)


def should_continue_writing(
    current_words: int,
    target_total_words: Optional[int],
    max_chapters: Optional[int],
    current_chapter_count: int,
) -> bool:
    """判断自动写作是否还应继续。"""
    if target_total_words is not None and current_words >= target_total_words:
        return False
    if max_chapters is not None and current_chapter_count >= max_chapters:
        return False
    return True


def select_chapters_for_batch(chapters: Iterable[Any], chapters_per_batch: int) -> List[Any]:
    """选择草稿或无内容章节，按章节序号排序并限制批次大小。"""
    candidates = [
        chapter for chapter in chapters
        if getattr(chapter, "status", None) == "draft"
        or not (getattr(chapter, "content", None) or "").strip()
    ]
    candidates.sort(key=lambda chapter: getattr(chapter, "chapter_number", 0))
    return candidates[:max(chapters_per_batch, 0)]


def should_pause_for_quality_failures(consecutive_failures: int, limit: int) -> bool:
    """连续质量失败达到阈值时暂停任务。"""
    return limit > 0 and consecutive_failures >= limit


def build_quality_failure_record(
    chapter_id: str,
    chapter_number: int,
    scores: QualityScores,
    gate_result: QualityGateResult,
    retry_count: int,
) -> Dict[str, Any]:
    """构造可序列化的质量失败记录。"""
    return {
        "chapter_id": chapter_id,
        "chapter_number": chapter_number,
        "scores": {
            "overall": scores.overall,
            "coherence": scores.coherence,
            "pacing": scores.pacing,
            "engagement": scores.engagement,
        },
        "failing_scores": gate_result.failing_scores,
        "retry_count": retry_count,
    }


def build_seed_chapter_plans_from_idea(task_input: Dict[str, Any], chapter_count: int) -> List[Dict[str, Any]]:
    """根据新想法输入生成可落库的初始章节草稿计划。"""
    if chapter_count <= 0:
        return []

    title = (task_input.get("title") or "未命名作品").strip()
    description = (task_input.get("description") or "围绕主角的核心选择与冲突展开。").strip()
    theme = (task_input.get("theme") or "成长与选择").strip()
    genre = (task_input.get("genre") or "通用").strip()
    beats = [
        ("开端", "交代主角处境、核心欲望与故事钩子，埋下主要矛盾。"),
        ("异变", "外部事件打破平衡，主角被迫进入新的行动轨道。"),
        ("选择", "主角面对代价明确的选择，人物关系和目标发生变化。"),
        ("冲突", "对手或环境压力升级，主线目标遭遇实质阻碍。"),
        ("转折", "关键信息揭露，主角对世界或自身的认知被改写。"),
        ("代价", "行动结果带来损失或牺牲，为后续更大冲突蓄力。"),
        ("推进", "主角整合资源主动出击，阶段性悬念继续扩大。"),
        ("悬念", "以新的危险、承诺或秘密收束本阶段剧情。"),
    ]

    plans: List[Dict[str, Any]] = []
    for index in range(1, chapter_count + 1):
        beat_name, beat_goal = beats[(index - 1) % len(beats)]
        chapter_title = f"第{index}章：{beat_name}"
        summary = (
            f"《{title}》第{index}章围绕“{description}”展开。"
            f"本章类型为{genre}，主题聚焦{theme}。{beat_goal}"
        )
        structure = {
            "chapter_number": index,
            "title": chapter_title,
            "project_title": title,
            "genre": genre,
            "theme": theme,
            "summary": summary,
            "scenes": [
                "用具体行动呈现主角当前处境",
                "让冲突在场景中升级并留下后续钩子",
            ],
            "characters": [],
            "key_points": [
                "推进主线冲突",
                "保持人物动机清晰",
                "结尾留下下一章驱动力",
            ],
            "emotion": "紧张递进",
            "goal": beat_goal,
        }
        plans.append(
            {
                "chapter_number": index,
                "title": chapter_title,
                "summary": summary,
                "structure": structure,
            }
        )
    return plans


def _quality_config_from_task_input(task_input: Dict[str, Any]) -> QualityGateConfig:
    quality = task_input.get("quality") or {}
    return QualityGateConfig(
        overall_threshold=quality.get("overall_threshold", 7.5),
        coherence_threshold=quality.get("coherence_threshold", 7.0),
        pacing_threshold=quality.get("pacing_threshold"),
        engagement_threshold=quality.get("engagement_threshold"),
    )


def _scores_from_analysis(analysis: Optional[PlotAnalysis]) -> QualityScores:
    if analysis is None:
        return QualityScores(overall=0.0, coherence=0.0, pacing=0.0, engagement=0.0)
    return QualityScores(
        overall=float(analysis.overall_quality_score or 0.0),
        coherence=float(analysis.coherence_score or 0.0),
        pacing=float(analysis.pacing_score or 0.0),
        engagement=float(analysis.engagement_score or 0.0),
    )


async def run_auto_writing_background(task_id: str, user_id: str) -> None:
    """自动写作后台任务入口。"""
    tracker = TaskProgressTracker(task_id, user_id, "自动写作")

    engine = await get_engine(user_id)
    AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with AsyncSessionLocal() as db:
            task = await _load_task(db, task_id, user_id)
            if not task:
                await tracker.error("自动写作任务不存在")
                return
            if task.status == "paused":
                return
            if task.cancel_requested or task.status == "cancelled":
                await _mark_task_cancelled(db, task)
                return

            await tracker.start("开始自动写作...")

            task_input = task.task_input or {}
            project = await db.get(Project, task.project_id)
            if not project:
                await _mark_task_failed(db, task, "项目不存在")
                return

            if task_input.get("mode", "existing_project") == "new_idea":
                await tracker.loading("创建初始大纲与章节草稿...", 0.2)
                await ensure_seed_chapters_for_new_idea(db, project, task_input)

            await tracker.loading("加载章节列表...", 0.4)
            await _run_writing_loop(db, task, project, user_id, tracker)

    except Exception as exc:
        logger.error(f"自动写作任务失败: {task_id}", exc_info=True)
        await tracker.error(str(exc))


async def ensure_seed_chapters_for_new_idea(
    db: AsyncSession,
    project: Project,
    task_input: Dict[str, Any],
) -> int:
    """为新想法项目创建初始大纲和章节草稿，已有章节时不重复创建。"""
    existing_result = await db.execute(
        select(Chapter.id).where(Chapter.project_id == project.id).limit(1)
    )
    if existing_result.scalar_one_or_none():
        return 0

    target_words = int(task_input.get("target_total_words") or 100000)
    words_per_chapter = max(int(task_input.get("target_words_per_chapter") or 3000), 1)
    estimated_chapters = max(1, (target_words + words_per_chapter - 1) // words_per_chapter)
    seed_count = min(int(task_input.get("max_chapters") or estimated_chapters), 200)
    plans = build_seed_chapter_plans_from_idea(task_input, seed_count)
    for plan in plans:
        outline = Outline(
            project_id=project.id,
            title=plan["title"],
            content=plan["summary"],
            structure=json.dumps(plan["structure"], ensure_ascii=False),
            order_index=plan["chapter_number"],
        )
        db.add(outline)
        await db.flush()
        chapter = Chapter(
            project_id=project.id,
            chapter_number=plan["chapter_number"],
            title=plan["title"],
            summary=plan["summary"],
            content="",
            word_count=0,
            status="draft",
            outline_id=outline.id,
            sub_index=1,
            expansion_plan=json.dumps(plan["structure"], ensure_ascii=False),
        )
        db.add(chapter)

    project.wizard_status = "completed"
    project.wizard_step = 4
    project.status = "writing"
    project.chapter_count = max(project.chapter_count or 0, len(plans))
    await db.commit()
    return len(plans)


async def _run_writing_loop(
    db: AsyncSession,
    task: BackgroundTask,
    project: Project,
    user_id: str,
    tracker: TaskProgressTracker,
) -> None:
    previous_result = task.task_result or {}
    total_generated = int(previous_result.get("generated_chapters") or 0)
    previous_failures = previous_result.get("quality_failures")
    quality_failures: List[Dict[str, Any]] = (
        list(previous_failures) if isinstance(previous_failures, list) else []
    )
    consecutive_failures = 0

    while True:
        await db.refresh(task)
        await db.refresh(project)
        if task.cancel_requested or task.status == "cancelled":
            await _mark_task_cancelled(db, task)
            return
        if task.status == "paused":
            return

        batch_result = await _run_existing_project_batch(
            db=db,
            task=task,
            project=project,
            user_id=user_id,
            tracker=tracker,
            existing_quality_failures=quality_failures,
            starting_consecutive_failures=consecutive_failures,
            starting_generated_count=total_generated,
        )
        total_generated += batch_result["generated_count"]
        quality_failures = batch_result["quality_failures"]
        consecutive_failures = batch_result["consecutive_failures"]

        if batch_result["status"] == "continue":
            continue
        return


async def _run_existing_project_batch(
    db: AsyncSession,
    task: BackgroundTask,
    project: Project,
    user_id: str,
    tracker: TaskProgressTracker,
    existing_quality_failures: Optional[List[Dict[str, Any]]] = None,
    starting_consecutive_failures: int = 0,
    starting_generated_count: int = 0,
) -> Dict[str, Any]:
    task_input = task.task_input or {}
    quality_input = task_input.get("quality") or {}
    quality_config = _quality_config_from_task_input(task_input)
    chapters_per_batch = int(task_input.get("chapters_per_batch") or 1)
    target_words_per_chapter = int(task_input.get("target_words_per_chapter") or 3000)
    max_quality_retries = int(quality_input.get("max_quality_retries", 1))
    failure_limit = int(quality_input.get("consecutive_failure_limit", 3))

    result = await db.execute(
        select(Chapter)
        .where(Chapter.project_id == project.id)
        .order_by(Chapter.chapter_number)
    )
    chapters = list(result.scalars().all())

    if not should_continue_writing(
        current_words=project.current_words or 0,
        target_total_words=task_input.get("target_total_words"),
        max_chapters=task_input.get("max_chapters"),
        current_chapter_count=starting_generated_count,
    ):
        await _mark_task_completed(
            db,
            task,
            "自动写作完成：已达到目标字数或章节数",
            {
                "message": "已达到目标字数或章节数",
                "generated_chapters": starting_generated_count,
                "quality_failures": existing_quality_failures or [],
            },
        )
        return {
            "status": "completed",
            "generated_count": 0,
            "quality_failures": existing_quality_failures or [],
            "consecutive_failures": starting_consecutive_failures,
        }

    max_chapters = task_input.get("max_chapters")
    if max_chapters is not None:
        remaining_chapters = max(int(max_chapters) - starting_generated_count, 0)
        chapters_per_batch = min(chapters_per_batch, remaining_chapters)

    selected_chapters = select_chapters_for_batch(chapters, chapters_per_batch)
    if not selected_chapters:
        await _mark_task_completed(
            db,
            task,
            "没有可生成章节，请先生成或展开大纲",
            {
                "message": "没有可生成章节，请先生成或展开大纲",
                "generated_chapters": starting_generated_count,
                "quality_failures": existing_quality_failures or [],
            },
        )
        return {
            "status": "completed",
            "generated_count": 0,
            "quality_failures": existing_quality_failures or [],
            "consecutive_failures": starting_consecutive_failures,
        }

    await tracker.preparing(f"准备生成 {len(selected_chapters)} 个章节...")

    from app.api.chapters import (
        analyze_chapter_background,
        generate_single_chapter_for_batch,
        get_db_write_lock,
    )
    from app.api.settings import get_user_ai_service_from_db_by_usage

    ai_service = await get_user_ai_service_from_db_by_usage(
        user_id=user_id,
        db=db,
        usage="chapter_generation",
    )
    write_lock = await get_db_write_lock(user_id)
    previous_summary_context = None
    generated_count = 0
    consecutive_failures = starting_consecutive_failures
    quality_failures: List[Dict[str, Any]] = list(existing_quality_failures or [])

    for index, chapter in enumerate(selected_chapters, start=1):
        await db.refresh(task)
        if task.cancel_requested or task.status == "cancelled":
            await _mark_task_cancelled(db, task)
            return {
                "status": "cancelled",
                "generated_count": generated_count,
                "quality_failures": quality_failures,
                "consecutive_failures": consecutive_failures,
            }
        if task.status == "paused":
            return {
                "status": "paused",
                "generated_count": generated_count,
                "quality_failures": quality_failures,
                "consecutive_failures": consecutive_failures,
            }

        await tracker.generating(
            current_chars=index - 1,
            estimated_total=len(selected_chapters),
            message=f"生成第 {chapter.chapter_number} 章 ({index}/{len(selected_chapters)})",
        )
        previous_summary_context = await generate_single_chapter_for_batch(
            db_session=db,
            chapter=chapter,
            user_id=user_id,
            style_id=task_input.get("style_id"),
            target_word_count=target_words_per_chapter,
            ai_service=ai_service,
            write_lock=write_lock,
            custom_model=task_input.get("model"),
            previous_summary_context=previous_summary_context,
        )
        generated_count += 1

        analysis = await _analyze_and_load_latest(
            db=db,
            chapter=chapter,
            user_id=user_id,
        )
        scores = _scores_from_analysis(analysis)
        gate_result = evaluate_quality_gate(scores, quality_config)

        retry_count = 0
        while not gate_result.passed and retry_count < max_quality_retries:
            retry_count += 1
            await tracker.retry(retry_count, max_quality_retries, "章节质量未达标，自动重写")
            analysis = await _regenerate_apply_and_analyze(
                db=db,
                chapter=chapter,
                analysis=analysis,
                user_id=user_id,
                ai_service=ai_service,
                target_word_count=target_words_per_chapter,
                style_id=task_input.get("style_id"),
            )
            scores = _scores_from_analysis(analysis)
            gate_result = evaluate_quality_gate(scores, quality_config)

        if gate_result.passed:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            quality_failures.append(
                build_quality_failure_record(
                    chapter_id=chapter.id,
                    chapter_number=chapter.chapter_number,
                    scores=scores,
                    gate_result=gate_result,
                    retry_count=retry_count,
                )
            )
            if should_pause_for_quality_failures(consecutive_failures, failure_limit):
                await _mark_task_paused(
                    db,
                    task,
                    f"连续 {consecutive_failures} 章质量未达标，任务已暂停",
                    starting_generated_count + generated_count,
                    quality_failures,
                )
                return {
                    "status": "paused",
                    "generated_count": generated_count,
                    "quality_failures": quality_failures,
                    "consecutive_failures": consecutive_failures,
                }

        task.progress = min(95, int(index / len(selected_chapters) * 95))
        task.status_message = f"已完成 {index}/{len(selected_chapters)} 章"
        task.task_result = {
            "message": "自动写作进行中",
            "generated_chapters": starting_generated_count + generated_count,
            "quality_failures": quality_failures,
        }
        task.updated_at = datetime.now()
        await db.commit()

    return {
        "status": "continue",
        "generated_count": generated_count,
        "quality_failures": quality_failures,
        "consecutive_failures": consecutive_failures,
    }


async def _load_task(db: AsyncSession, task_id: str, user_id: str) -> Optional[BackgroundTask]:
    result = await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.id == task_id,
            BackgroundTask.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _analyze_and_load_latest(
    db: AsyncSession,
    chapter: Chapter,
    user_id: str,
) -> Optional[PlotAnalysis]:
    from app.api.chapters import analyze_chapter_background

    task = AnalysisTask(
        chapter_id=chapter.id,
        user_id=user_id,
        project_id=chapter.project_id,
        status="pending",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    await analyze_chapter_background(
        chapter_id=chapter.id,
        user_id=user_id,
        project_id=chapter.project_id,
        task_id=task.id,
    )
    return await _load_latest_analysis(db, chapter.id)


async def _load_latest_analysis(db: AsyncSession, chapter_id: str) -> Optional[PlotAnalysis]:
    result = await db.execute(
        select(PlotAnalysis)
        .where(PlotAnalysis.chapter_id == chapter_id)
        .order_by(PlotAnalysis.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _regenerate_apply_and_analyze(
    db: AsyncSession,
    chapter: Chapter,
    analysis: Optional[PlotAnalysis],
    user_id: str,
    ai_service: Any,
    target_word_count: int,
    style_id: Optional[int],
) -> Optional[PlotAnalysis]:
    from app.services.chapter_regenerator import ChapterRegenerator

    project_context = await _build_regeneration_context(db, chapter)
    style_content = await _load_style_content(db, style_id, user_id)
    request = ChapterRegenerateRequest(
        modification_source="mixed" if analysis and analysis.suggestions else "custom",
        selected_suggestion_indices=list(range(min(len(analysis.suggestions or []), 3))) if analysis else None,
        custom_instructions="请根据质量评分和分析建议自动重写本章，重点提升整体质量与连贯性。",
        preserve_elements=PreserveElementsConfig(preserve_structure=True, preserve_character_traits=True),
        style_id=style_id,
        target_word_count=target_word_count,
        focus_areas=["pacing", "description", "conflict"],
        auto_apply=True,
    )

    regenerator = ChapterRegenerator(ai_service)
    full_content = ""
    async for event in regenerator.regenerate_with_feedback(
        chapter=chapter,
        analysis=analysis,
        regenerate_request=request,
        project_context=project_context,
        style_content=style_content,
        user_id=user_id,
        db=db,
    ):
        if event.get("type") == "chunk":
            full_content += event.get("content", "")

    if full_content.strip():
        old_word_count = chapter.word_count or 0
        original_content = chapter.content
        chapter.content = full_content
        chapter.word_count = len(full_content)
        chapter.status = "completed"

        project = await db.get(Project, chapter.project_id)
        if project:
            project.current_words = (project.current_words or 0) - old_word_count + chapter.word_count

        regen_task = RegenerationTask(
            chapter_id=chapter.id,
            analysis_id=analysis.id if analysis else None,
            user_id=user_id,
            project_id=chapter.project_id,
            modification_instructions=request.custom_instructions or "",
            original_suggestions=analysis.suggestions if analysis else None,
            selected_suggestion_indices=request.selected_suggestion_indices,
            custom_instructions=request.custom_instructions,
            style_id=style_id,
            target_word_count=target_word_count,
            focus_areas=request.focus_areas,
            preserve_elements=request.preserve_elements.model_dump() if request.preserve_elements else None,
            status="completed",
            progress=100,
            original_content=original_content,
            original_word_count=old_word_count,
            regenerated_content=full_content,
            regenerated_word_count=len(full_content),
            version_note="自动写作质量门重写",
            started_at=datetime.now(),
            completed_at=datetime.now(),
        )
        db.add(regen_task)
        await db.commit()
        await db.refresh(chapter)

    return await _analyze_and_load_latest(db, chapter, user_id)


async def _build_regeneration_context(db: AsyncSession, chapter: Chapter) -> Dict[str, Any]:
    project = await db.get(Project, chapter.project_id)
    outline = None
    if chapter.outline_id:
        outline = await db.get(Outline, chapter.outline_id)

    return {
        "project_title": project.title if project else "未知",
        "genre": project.genre if project else "未设定",
        "theme": project.theme if project else "未设定",
        "narrative_perspective": project.narrative_perspective if project else "第三人称",
        "time_period": project.world_time_period if project else "未设定",
        "location": project.world_location if project else "未设定",
        "atmosphere": project.world_atmosphere if project else "未设定",
        "characters_info": "请保持既有人设一致",
        "chapter_outline": outline.content if outline else chapter.summary or "暂无大纲",
        "previous_context": "",
    }


async def _load_style_content(db: AsyncSession, style_id: Optional[int], user_id: str) -> str:
    if not style_id:
        return ""
    result = await db.execute(select(WritingStyle).where(WritingStyle.id == style_id))
    style = result.scalar_one_or_none()
    if not style:
        return ""
    if style.user_id is not None and style.user_id != user_id:
        return ""
    return style.prompt_content or ""


async def _mark_task_completed(
    db: AsyncSession,
    task: BackgroundTask,
    message: str,
    result: Dict[str, Any],
) -> None:
    task.status = "completed"
    task.progress = 100
    task.status_message = message
    task.task_result = result
    task.completed_at = datetime.now()
    task.updated_at = datetime.now()
    await db.commit()


async def _mark_task_failed(db: AsyncSession, task: BackgroundTask, message: str) -> None:
    task.status = "failed"
    task.error_message = message
    task.status_message = f"失败: {message}"
    task.completed_at = datetime.now()
    task.updated_at = datetime.now()
    await db.commit()


async def _mark_task_cancelled(db: AsyncSession, task: BackgroundTask) -> None:
    task.status = "cancelled"
    task.cancel_requested = True
    task.status_message = "任务已取消"
    task.completed_at = datetime.now()
    task.updated_at = datetime.now()
    await db.commit()


async def _mark_task_paused(
    db: AsyncSession,
    task: BackgroundTask,
    message: str,
    generated_count: int,
    quality_failures: List[Dict[str, Any]],
) -> None:
    task.status = "paused"
    task.status_message = message
    task.task_result = {
        "message": message,
        "generated_chapters": generated_count,
        "quality_failures": quality_failures,
    }
    task.progress_details = {"stage": "paused", "message": message}
    task.updated_at = datetime.now()
    await db.commit()
