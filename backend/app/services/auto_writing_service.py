"""自动写作后台编排服务"""
from __future__ import annotations

import json
import asyncio
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
from app.models.character import Character
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


def calculate_seed_chapter_count(task_input: Dict[str, Any], cap: int = 200) -> int:
    """按目标字数估算需要预置的章节草稿数量。"""
    if task_input.get("max_chapters") is not None:
        return max(1, min(int(task_input["max_chapters"]), cap))

    target_words = int(task_input.get("target_total_words") or 100000)
    words_per_chapter = max(int(task_input.get("target_words_per_chapter") or 3000), 1)
    return max(1, min((target_words + words_per_chapter - 1) // words_per_chapter, cap))


def build_auto_writing_planning_prompt(task_input: Dict[str, Any], chapter_count: int) -> str:
    """构建新想法自动写作的一次性规划提示词。"""
    title = task_input.get("title") or "未命名作品"
    description = task_input.get("description") or "暂无简介"
    theme = task_input.get("theme") or "成长与选择"
    genre = task_input.get("genre") or "通用"
    return f"""你是长篇小说总策划。请根据用户的灵感，先完成世界观、主要人物和可直接用于章节生成的章节计划。

作品标题：{title}
类型：{genre}
主题：{theme}
灵感描述：{description}

请严格返回 JSON，不要输出 Markdown、解释或代码块。JSON 结构如下：
{{
  "world": {{
    "time_period": "时间背景",
    "location": "主要地点",
    "atmosphere": "氛围基调",
    "rules": "世界规则"
  }},
  "characters": [
    {{
      "name": "角色名",
      "age": "年龄或阶段",
      "gender": "性别",
      "role_type": "protagonist/supporting/antagonist",
      "personality": "性格特点",
      "background": "背景故事",
      "appearance": "外貌或辨识特征",
      "traits": ["标签1", "标签2"]
    }}
  ],
  "chapters": [
    {{
      "title": "章节标题",
      "summary": "章节摘要",
      "goal": "本章叙事目标",
      "emotion": "情绪基调",
      "scenes": ["关键场景1", "关键场景2"],
      "characters": ["本章重点角色名"],
      "key_points": ["必须完成的剧情点"]
    }}
  ]
}}

要求：
1. 生成 {chapter_count} 个章节计划，覆盖故事从开局到阶段性推进，不要过早完结。
2. 每章必须有可执行的冲突、目标和结尾钩子，能直接交给章节生成模型创作。
3. characters 里至少包含主角、关键盟友或对手；章节 characters 请引用这些角色名。
4. 内容要与类型、主题和灵感描述一致。"""


def _coerce_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _chapter_title_for_number(title: Optional[str], chapter_number: int) -> str:
    """确保章节标题与落库序号一致。"""
    clean = _coerce_text(title, f"第{chapter_number}章")
    if clean.startswith(f"第{chapter_number}章"):
        return clean
    import re

    stripped = re.sub(r"^第\s*\d+\s*章[：:、\-\s]*", "", clean).strip()
    return f"第{chapter_number}章：{stripped or '续章'}"


def normalize_status_value(value: Any) -> Optional[str]:
    """将任务状态标准化为可判断的字符串。"""
    if value is None:
        return None
    return str(value).strip().lower()


def is_pause_or_cancel_requested(task: Any) -> bool:
    """判断任务是否已被暂停或取消。"""
    status = normalize_status_value(getattr(task, "status", None))
    return bool(getattr(task, "cancel_requested", False)) or status in {"paused", "cancelled"}


def _extract_json_data(raw_text: str) -> Any:
    """从 AI 返回中尽量解析 JSON。"""
    from app.services.json_helper import loads_json

    text = (raw_text or "").strip()
    if not text:
        raise ValueError("empty AI response")
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        return loads_json(text)
    except Exception:
        start_candidates = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
        if not start_candidates:
            raise
        start = min(start_candidates)
        end = max(text.rfind("}"), text.rfind("]"))
        if end <= start:
            raise
        return loads_json(text[start:end + 1])


def _normalize_chapter_plan(
    raw: Dict[str, Any],
    index: int,
    task_input: Dict[str, Any],
) -> Dict[str, Any]:
    title = _coerce_text(raw.get("title"), f"第{index}章")
    if not title.startswith(f"第{index}章"):
        title = f"第{index}章：{title}"
    summary = _coerce_text(
        raw.get("summary") or raw.get("content"),
        f"本章承接《{task_input.get('title') or '未命名作品'}》主线，推进核心冲突。",
    )
    structure = {
        "chapter_number": index,
        "title": title,
        "project_title": task_input.get("title") or "未命名作品",
        "genre": task_input.get("genre") or "通用",
        "theme": task_input.get("theme") or "成长与选择",
        "summary": summary,
        "scenes": raw.get("scenes") if isinstance(raw.get("scenes"), list) else [],
        "characters": raw.get("characters") if isinstance(raw.get("characters"), list) else [],
        "key_points": raw.get("key_points") if isinstance(raw.get("key_points"), list) else [],
        "emotion": _coerce_text(raw.get("emotion"), "紧张递进"),
        "goal": _coerce_text(raw.get("goal"), "推进主线冲突并留下后续钩子"),
    }
    return {
        "chapter_number": index,
        "title": title,
        "summary": summary,
        "structure": structure,
    }


def extract_auto_writing_plan_from_ai_response(
    raw_text: str,
    task_input: Dict[str, Any],
    chapter_count: int,
) -> Dict[str, Any]:
    """解析 AI 规划，失败时回退到规则章节计划。"""
    fallback_chapters = build_seed_chapter_plans_from_idea(task_input, chapter_count)
    fallback_world = {
        "time_period": "由故事开局逐步揭示",
        "location": task_input.get("genre") or "核心舞台",
        "atmosphere": task_input.get("theme") or "紧张递进",
        "rules": task_input.get("description") or "围绕主角目标和核心冲突展开",
    }
    try:
        data = _extract_json_data(raw_text)
        if isinstance(data, list):
            data = {"chapters": data}
        if not isinstance(data, dict):
            raise ValueError("AI plan is not an object")

        raw_chapters = data.get("chapters")
        if not isinstance(raw_chapters, list) or not raw_chapters:
            raise ValueError("AI plan has no chapters")

        chapters = [
            _normalize_chapter_plan(item if isinstance(item, dict) else {}, idx, task_input)
            for idx, item in enumerate(raw_chapters[:chapter_count], start=1)
        ]
        if len(chapters) < chapter_count:
            for plan in fallback_chapters[len(chapters):chapter_count]:
                chapters.append(plan)

        world = data.get("world") if isinstance(data.get("world"), dict) else {}
        characters = data.get("characters") if isinstance(data.get("characters"), list) else []
        return {
            "source": "ai",
            "world": {
                "time_period": _coerce_text(world.get("time_period"), fallback_world["time_period"]),
                "location": _coerce_text(world.get("location"), fallback_world["location"]),
                "atmosphere": _coerce_text(world.get("atmosphere"), fallback_world["atmosphere"]),
                "rules": _coerce_text(world.get("rules"), fallback_world["rules"]),
            },
            "characters": characters,
            "chapters": chapters,
        }
    except Exception as exc:
        logger.warning(f"自动写作 AI 规划解析失败，使用规则计划: {exc}")
        return {
            "source": "fallback",
            "world": fallback_world,
            "characters": [],
            "chapters": fallback_chapters,
        }


def build_quality_retry_instruction(
    scores: QualityScores,
    gate_result: QualityGateResult,
    analysis: Optional[Any],
) -> str:
    """根据评分和分析建议生成更具体的质量重写指令。"""
    score_text = (
        f"overall={scores.overall:.1f}, coherence={scores.coherence:.1f}, "
        f"pacing={scores.pacing:.1f}, engagement={scores.engagement:.1f}"
    )
    suggestions = []
    if analysis and getattr(analysis, "suggestions", None):
        suggestions = [str(item) for item in (analysis.suggestions or [])[:5] if str(item).strip()]
    suggestion_text = "\n".join(f"- {item}" for item in suggestions) or "- 强化冲突推进、人物动机和章节结尾钩子"
    failing = ", ".join(gate_result.failing_scores) or "unknown"
    return f"""请根据自动质量门结果重写本章，必须解决以下低分项：{failing}。
当前评分：{score_text}
分析建议：
{suggestion_text}

重写要求：
1. 保留原章节核心事件、人物关系和大纲目标，不要偏离主线。
2. 优先修复低分项；若 coherence 低，补足因果衔接；若 pacing 低，压缩拖沓段落并加强行动；若 engagement 低，增强悬念和冲突。
3. 输出完整章节正文，不要输出解释、评分或修改说明。"""


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
                await ensure_seed_chapters_for_new_idea(db, project, task_input, user_id=user_id)

            await tracker.loading("加载章节列表...", 0.4)
            await _run_writing_loop(db, task, project, user_id, tracker)

    except Exception as exc:
        logger.error(f"自动写作任务失败: {task_id}", exc_info=True)
        await tracker.error(str(exc))


async def ensure_seed_chapters_for_new_idea(
    db: AsyncSession,
    project: Project,
    task_input: Dict[str, Any],
    user_id: Optional[str] = None,
    ai_service: Optional[Any] = None,
) -> int:
    """为新想法项目创建初始大纲和章节草稿，已有章节时不重复创建。"""
    existing_result = await db.execute(
        select(Chapter.id).where(Chapter.project_id == project.id).limit(1)
    )
    if existing_result.scalar_one_or_none():
        return 0

    seed_count = calculate_seed_chapter_count(task_input)
    plan = await _build_initial_story_plan(
        db=db,
        project=project,
        task_input=task_input,
        chapter_count=seed_count,
        user_id=user_id,
        ai_service=ai_service,
    )
    await _apply_story_plan_to_project(project, plan)
    created = await _create_outline_chapters_from_plans(db, project, plan["chapters"], start_number=1)
    if ai_service is None and user_id:
        try:
            from app.api.settings import get_user_ai_service_from_db_by_usage

            ai_service = await get_user_ai_service_from_db_by_usage(user_id=user_id, db=db, usage="default")
        except Exception as exc:
            logger.warning(f"自动写作初始化角色补全获取 AI 服务失败: {exc}")
    await _ensure_characters_from_story_plan(db, project, plan, user_id, ai_service)

    project.wizard_status = "completed"
    project.wizard_step = 4
    project.status = "writing"
    project.chapter_count = max(project.chapter_count or 0, created)
    await db.commit()
    return created


async def _build_initial_story_plan(
    db: AsyncSession,
    project: Project,
    task_input: Dict[str, Any],
    chapter_count: int,
    user_id: Optional[str],
    ai_service: Optional[Any] = None,
) -> Dict[str, Any]:
    if ai_service is None and user_id:
        try:
            from app.api.settings import get_user_ai_service_from_db_by_usage

            ai_service = await get_user_ai_service_from_db_by_usage(user_id=user_id, db=db, usage="default")
        except Exception as exc:
            logger.warning(f"自动写作初始化获取 AI 服务失败，使用规则计划: {exc}")

    if ai_service is None:
        return extract_auto_writing_plan_from_ai_response("", task_input, chapter_count)

    try:
        prompt = build_auto_writing_planning_prompt(task_input, chapter_count)
        planning_data = await ai_service.call_with_json_retry(
            prompt=prompt,
            max_retries=3,
            max_tokens=16000,
            model=task_input.get("model"),
            expected_type="object",
        )
        return extract_auto_writing_plan_from_ai_response(
            json.dumps(planning_data, ensure_ascii=False),
            task_input,
            chapter_count,
        )
    except Exception as exc:
        logger.warning(f"自动写作 AI 初始化规划失败，使用规则计划: {exc}")
        return extract_auto_writing_plan_from_ai_response("", task_input, chapter_count)


async def _apply_story_plan_to_project(project: Project, plan: Dict[str, Any]) -> None:
    world = plan.get("world") or {}
    if not project.world_time_period:
        project.world_time_period = world.get("time_period")
    if not project.world_location:
        project.world_location = world.get("location")
    if not project.world_atmosphere:
        project.world_atmosphere = world.get("atmosphere")
    if not project.world_rules:
        project.world_rules = world.get("rules")


async def _create_outline_chapters_from_plans(
    db: AsyncSession,
    project: Project,
    plans: List[Dict[str, Any]],
    start_number: int,
) -> int:
    created = 0
    for offset, plan in enumerate(plans):
        chapter_number = start_number + offset
        title = _chapter_title_for_number(plan.get("title"), chapter_number)
        structure = dict(plan.get("structure") or {})
        structure["chapter_number"] = chapter_number
        structure["title"] = title
        outline = Outline(
            project_id=project.id,
            title=title,
            content=plan["summary"],
            structure=json.dumps(structure, ensure_ascii=False),
            order_index=chapter_number,
        )
        db.add(outline)
        await db.flush()
        chapter = Chapter(
            project_id=project.id,
            chapter_number=chapter_number,
            title=title,
            summary=plan["summary"],
            content="",
            word_count=0,
            status="draft",
            outline_id=outline.id,
            sub_index=1,
            expansion_plan=json.dumps(structure, ensure_ascii=False),
        )
        db.add(chapter)
        created += 1

    await db.flush()
    return created


async def _ensure_characters_from_story_plan(
    db: AsyncSession,
    project: Project,
    plan: Dict[str, Any],
    user_id: Optional[str],
    ai_service: Optional[Any],
) -> None:
    characters = plan.get("characters") or []
    created_names = set()
    for raw in characters[:20]:
        if not isinstance(raw, dict):
            continue
        name = _coerce_text(raw.get("name"))
        if not name or name in created_names:
            continue
        existing = await db.execute(
            select(Character.id).where(Character.project_id == project.id, Character.name == name).limit(1)
        )
        if existing.scalar_one_or_none():
            continue
        character = Character(
            project_id=project.id,
            name=name,
            age=_coerce_text(raw.get("age"), None),
            gender=_coerce_text(raw.get("gender"), None),
            is_organization=bool(raw.get("is_organization", False)),
            role_type=_coerce_text(raw.get("role_type"), "supporting"),
            personality=_coerce_text(raw.get("personality"), None),
            background=_coerce_text(raw.get("background"), None),
            appearance=_coerce_text(raw.get("appearance"), None),
            traits=json.dumps(raw.get("traits"), ensure_ascii=False) if isinstance(raw.get("traits"), list) else None,
        )
        db.add(character)
        created_names.add(name)

    if ai_service and user_id:
        outline_items = [chapter.get("structure", {}) for chapter in (plan.get("chapters") or [])]
        try:
            from app.services.auto_character_service import get_auto_character_service
            from app.services.auto_organization_service import get_auto_organization_service

            await get_auto_character_service(ai_service).check_and_create_missing_characters(
                project_id=project.id,
                outline_data_list=outline_items,
                db=db,
                user_id=user_id,
                enable_mcp=True,
            )
            await get_auto_organization_service(ai_service).check_and_create_missing_organizations(
                project_id=project.id,
                outline_data_list=outline_items,
                db=db,
                user_id=user_id,
                enable_mcp=True,
            )
        except Exception as exc:
            logger.warning(f"自动写作初始化角色/组织补全失败，不影响主流程: {exc}")

    await db.flush()


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
        extended_count = await _extend_story_if_needed(
            db=db,
            project=project,
            task=task,
            user_id=user_id,
            task_input=task_input,
            starting_generated_count=starting_generated_count,
            tracker=tracker,
        )
        if extended_count > 0:
            return {
                "status": "continue",
                "generated_count": 0,
                "quality_failures": existing_quality_failures or [],
                "consecutive_failures": starting_consecutive_failures,
            }
        else:
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

        async def should_stop_generation() -> bool:
            await db.refresh(task)
            return is_pause_or_cancel_requested(task)

        try:
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
                should_stop=should_stop_generation,
            )
        except asyncio.CancelledError:
            await db.refresh(task)
            if task.cancel_requested or task.status == "cancelled":
                await _mark_task_cancelled(db, task)
                return {
                    "status": "cancelled",
                    "generated_count": generated_count,
                    "quality_failures": quality_failures,
                    "consecutive_failures": consecutive_failures,
                }
            return {
                "status": "paused",
                "generated_count": generated_count,
                "quality_failures": quality_failures,
                "consecutive_failures": consecutive_failures,
            }
        generated_count += 1

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
            analysis = await _regenerate_apply_and_analyze(
                db=db,
                chapter=chapter,
                analysis=analysis,
                user_id=user_id,
                ai_service=ai_service,
                target_word_count=target_words_per_chapter,
                style_id=task_input.get("style_id"),
                scores=scores,
                gate_result=gate_result,
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


async def _extend_story_if_needed(
    db: AsyncSession,
    project: Project,
    task: BackgroundTask,
    user_id: str,
    task_input: Dict[str, Any],
    starting_generated_count: int,
    tracker: TaskProgressTracker,
) -> int:
    """没有可写草稿时自动追加后续大纲与章节草稿。"""
    if task_input.get("auto_expand_outline", True) is False:
        return 0
    if not should_continue_writing(
        current_words=project.current_words or 0,
        target_total_words=task_input.get("target_total_words"),
        max_chapters=task_input.get("max_chapters"),
        current_chapter_count=starting_generated_count,
    ):
        return 0

    max_chapters = task_input.get("max_chapters")
    remaining = int(max_chapters) - starting_generated_count if max_chapters is not None else None
    if remaining is not None and remaining <= 0:
        return 0
    batch_size = int(task_input.get("chapters_per_batch") or 1)
    extend_count = min(batch_size, remaining) if remaining is not None else batch_size
    if extend_count <= 0:
        return 0

    await tracker.preparing(f"自动追加 {extend_count} 个后续章节大纲...")
    try:
        from app.api.settings import get_user_ai_service_from_db_by_usage

        ai_service = await get_user_ai_service_from_db_by_usage(user_id=user_id, db=db, usage="default")
        prompt = await _build_outline_extension_prompt(db, project, task_input, extend_count)
        outline_data = await ai_service.call_with_json_retry(
            prompt=prompt,
            max_retries=3,
            max_tokens=12000,
            model=task_input.get("model"),
            expected_type="array",
        )
        plans = [
            _normalize_chapter_plan(item if isinstance(item, dict) else {}, idx, task_input)
            for idx, item in enumerate(outline_data[:extend_count], start=1)
        ]
    except Exception as exc:
        logger.warning(f"自动追加后续大纲失败，使用规则计划: {exc}")
        plans = build_seed_chapter_plans_from_idea(task_input, extend_count)

    max_number_result = await db.execute(
        select(Chapter.chapter_number)
        .where(Chapter.project_id == project.id)
        .order_by(Chapter.chapter_number.desc())
        .limit(1)
    )
    last_number = max_number_result.scalar_one_or_none() or 0
    created = await _create_outline_chapters_from_plans(db, project, plans, start_number=last_number + 1)
    project.chapter_count = max(project.chapter_count or 0, last_number + created)
    await db.commit()
    return created


async def _build_outline_extension_prompt(
    db: AsyncSession,
    project: Project,
    task_input: Dict[str, Any],
    chapter_count: int,
) -> str:
    result = await db.execute(
        select(Outline)
        .where(Outline.project_id == project.id)
        .order_by(Outline.order_index)
    )
    outlines = result.scalars().all()
    recent = outlines[-10:]
    recent_text = []
    for outline in recent:
        recent_text.append(f"第{outline.order_index}章《{outline.title}》：{outline.content or ''}")

    chars_result = await db.execute(
        select(Character).where(Character.project_id == project.id).order_by(Character.created_at)
    )
    characters = chars_result.scalars().all()
    characters_info = "\n".join(
        f"- {char.name} ({'组织' if char.is_organization else char.role_type or '角色'}): {(char.personality or char.background or '')[:120]}"
        for char in characters[:30]
    ) or "暂无角色信息"

    start_chapter = (recent[-1].order_index if recent else 0) + 1
    end_chapter = start_chapter + chapter_count - 1
    return f"""你是长篇小说大纲续写规划师。请基于已有大纲，继续规划第{start_chapter}章到第{end_chapter}章，共{chapter_count}章。

项目信息：
书名：{project.title}
类型：{project.genre or task_input.get('genre') or '通用'}
主题：{project.theme or task_input.get('theme') or '成长与选择'}
叙事视角：{project.narrative_perspective or '第三人称'}
世界观：{project.world_time_period or '未设定'}；{project.world_location or '未设定'}；{project.world_atmosphere or '未设定'}；{project.world_rules or '未设定'}

最近大纲：
{chr(10).join(recent_text) or '暂无'}

角色信息：
{characters_info}

请严格返回 JSON 数组，不要输出 Markdown。数组中必须有 {chapter_count} 个对象，每个对象包含：
chapter_number, title, summary, scenes, characters, key_points, emotion, goal。
要求：自然延续最近剧情，制造新冲突和钩子，不要重复前文，不要过早完结。"""


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
    scores: Optional[QualityScores] = None,
    gate_result: Optional[QualityGateResult] = None,
) -> Optional[PlotAnalysis]:
    from app.services.chapter_regenerator import ChapterRegenerator

    project_context = await _build_regeneration_context(db, chapter)
    style_content = await _load_style_content(db, style_id, user_id)
    request = ChapterRegenerateRequest(
        modification_source="mixed" if analysis and analysis.suggestions else "custom",
        selected_suggestion_indices=list(range(min(len(analysis.suggestions or []), 3))) if analysis else None,
        custom_instructions=build_quality_retry_instruction(
            scores=scores or _scores_from_analysis(analysis),
            gate_result=gate_result or QualityGateResult(passed=False, failing_scores=["overall"]),
            analysis=analysis,
        ),
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
