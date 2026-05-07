import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.auto_writing_service import (
    QualityGateConfig,
    QualityScores,
    build_auto_writing_planning_prompt,
    build_seed_chapter_plans_from_idea,
    build_quality_failure_record,
    build_quality_retry_instruction,
    calculate_seed_chapter_count,
    extract_auto_writing_plan_from_ai_response,
    _chapter_title_for_number,
    evaluate_quality_gate,
    is_pause_or_cancel_requested,
    select_chapters_for_batch,
    should_continue_writing,
    should_pause_for_quality_failures,
)
from app.schemas.auto_writing import AutoWritingStartRequest


def test_quality_gate_passes_when_required_scores_meet_thresholds():
    result = evaluate_quality_gate(
        QualityScores(overall=8.0, coherence=7.1, pacing=6.0, engagement=6.0),
        QualityGateConfig(overall_threshold=7.5, coherence_threshold=7.0),
    )

    assert result.passed is True
    assert result.failing_scores == []


def test_quality_gate_reports_failed_required_scores():
    result = evaluate_quality_gate(
        QualityScores(overall=7.0, coherence=6.5, pacing=8.0, engagement=8.0),
        QualityGateConfig(overall_threshold=7.5, coherence_threshold=7.0),
    )

    assert result.passed is False
    assert result.failing_scores == ["overall", "coherence"]


def test_quality_gate_uses_optional_thresholds_only_when_configured():
    result = evaluate_quality_gate(
        QualityScores(overall=8.0, coherence=8.0, pacing=6.0, engagement=6.0),
        QualityGateConfig(
            overall_threshold=7.5,
            coherence_threshold=7.0,
            pacing_threshold=7.0,
        ),
    )

    assert result.passed is False
    assert result.failing_scores == ["pacing"]


def test_select_chapters_for_batch_returns_draft_and_empty_chapters_in_order():
    chapters = [
        SimpleNamespace(id="3", chapter_number=3, status="completed", content="已有内容"),
        SimpleNamespace(id="1", chapter_number=1, status="draft", content=""),
        SimpleNamespace(id="2", chapter_number=2, status="completed", content="   "),
    ]

    selected = select_chapters_for_batch(chapters, chapters_per_batch=5)

    assert [chapter.id for chapter in selected] == ["1", "2"]


def test_select_chapters_for_batch_limits_to_configured_batch_size():
    chapters = [
        SimpleNamespace(id="1", chapter_number=1, status="draft", content=""),
        SimpleNamespace(id="2", chapter_number=2, status="draft", content=""),
        SimpleNamespace(id="3", chapter_number=3, status="draft", content=""),
    ]

    selected = select_chapters_for_batch(chapters, chapters_per_batch=2)

    assert [chapter.id for chapter in selected] == ["1", "2"]


def test_should_continue_writing_stops_when_current_words_reach_target():
    assert (
        should_continue_writing(
            current_words=10000,
            target_total_words=10000,
            max_chapters=None,
            current_chapter_count=4,
        )
        is False
    )


def test_should_pause_for_quality_failures_reaches_configured_limit():
    assert should_pause_for_quality_failures(consecutive_failures=2, limit=2) is True
    assert should_pause_for_quality_failures(consecutive_failures=1, limit=2) is False


def test_build_quality_failure_record_serializes_chapter_scores_and_retries():
    scores = QualityScores(overall=6.0, coherence=6.5, pacing=8.0, engagement=7.0)
    record = build_quality_failure_record(
        chapter_id="chapter-1",
        chapter_number=7,
        scores=scores,
        gate_result=evaluate_quality_gate(
            scores,
            QualityGateConfig(overall_threshold=7.5, coherence_threshold=7.0),
        ),
        retry_count=1,
    )

    assert record == {
        "chapter_id": "chapter-1",
        "chapter_number": 7,
        "scores": {
            "overall": 6.0,
            "coherence": 6.5,
            "pacing": 8.0,
            "engagement": 7.0,
        },
        "failing_scores": ["overall", "coherence"],
        "retry_count": 1,
    }


def test_start_request_accepts_frontend_quality_config_key():
    request = AutoWritingStartRequest.model_validate(
        {
            "mode": "existing_project",
            "project_id": "project-1",
            "target_total_words": 50000,
            "chapters_per_batch": 3,
            "target_words_per_chapter": 3000,
            "quality_config": {
                "overall_threshold": 8.2,
                "coherence_threshold": 7.8,
                "max_quality_retries": 2,
                "consecutive_quality_failure_limit": 4,
            },
        }
    )

    assert request.quality.overall_threshold == 8.2
    assert request.quality.coherence_threshold == 7.8
    assert request.quality.max_quality_retries == 2
    assert request.quality.consecutive_failure_limit == 4


def test_quality_defaults_match_auto_writing_design():
    request = AutoWritingStartRequest.model_validate(
        {
            "mode": "existing_project",
            "project_id": "project-1",
            "target_total_words": 50000,
            "chapters_per_batch": 3,
            "target_words_per_chapter": 3000,
        }
    )

    assert request.quality.overall_threshold == 7.5
    assert request.quality.coherence_threshold == 7.0
    assert request.quality.max_quality_retries == 2
    assert request.quality.consecutive_failure_limit == 3


def test_build_seed_chapter_plans_from_idea_uses_story_setup():
    plans = build_seed_chapter_plans_from_idea(
        {
            "title": "星海旧约",
            "description": "流亡舰队在失落星门前发现旧文明遗产。",
            "theme": "信任与牺牲",
            "genre": "科幻",
        },
        chapter_count=3,
    )

    assert [plan["chapter_number"] for plan in plans] == [1, 2, 3]
    assert plans[0]["title"].startswith("第1章")
    assert "星海旧约" in plans[0]["summary"]
    assert "流亡舰队" in plans[0]["summary"]
    assert plans[0]["structure"]["genre"] == "科幻"
    assert plans[1]["structure"]["goal"]


def test_build_seed_chapter_plans_limits_to_positive_count():
    assert build_seed_chapter_plans_from_idea({}, chapter_count=0) == []


def test_calculate_seed_chapter_count_uses_target_words_and_cap():
    assert calculate_seed_chapter_count({"target_total_words": 12000, "target_words_per_chapter": 3000}) == 4
    assert calculate_seed_chapter_count({"target_total_words": 900000, "target_words_per_chapter": 1000}) == 200
    assert calculate_seed_chapter_count({"max_chapters": 6, "target_total_words": 50000}) == 6


def test_extract_auto_writing_plan_accepts_ai_json_object():
    plan = extract_auto_writing_plan_from_ai_response(
        """
        {
          "world": {"time_period": "未来", "location": "星门边境", "atmosphere": "冷峻", "rules": "跃迁受限"},
          "characters": [{"name": "林澈", "role_type": "protagonist", "personality": "谨慎"}],
          "chapters": [{"title": "失落信标", "summary": "舰队发现异常信号", "goal": "引出主线"}]
        }
        """,
        {"title": "星海旧约", "genre": "科幻", "theme": "信任"},
        chapter_count=1,
    )

    assert plan["world"]["location"] == "星门边境"
    assert plan["characters"][0]["name"] == "林澈"
    assert plan["chapters"][0]["title"].startswith("第1章")
    assert plan["chapters"][0]["structure"]["goal"] == "引出主线"


def test_extract_auto_writing_plan_falls_back_when_json_invalid():
    plan = extract_auto_writing_plan_from_ai_response(
        "这不是 JSON",
        {"title": "星海旧约", "description": "舰队发现遗产"},
        chapter_count=2,
    )

    assert plan["source"] == "fallback"
    assert len(plan["chapters"]) == 2
    assert plan["world"]["rules"]


def test_build_auto_writing_planning_prompt_requires_structured_json():
    prompt = build_auto_writing_planning_prompt(
        {"title": "星海旧约", "description": "舰队发现遗产", "genre": "科幻", "theme": "信任"},
        chapter_count=5,
    )

    assert "严格返回 JSON" in prompt
    assert "5 个章节计划" in prompt
    assert "星海旧约" in prompt


def test_build_quality_retry_instruction_includes_failed_scores_and_suggestions():
    analysis = SimpleNamespace(
        suggestions=["加强冲突", "补足人物动机"],
        pacing_score=6.0,
        coherence_score=6.5,
    )
    instruction = build_quality_retry_instruction(
        scores=QualityScores(overall=6.0, coherence=6.5, pacing=6.0, engagement=7.0),
        gate_result=evaluate_quality_gate(
            QualityScores(overall=6.0, coherence=6.5, pacing=6.0, engagement=7.0),
            QualityGateConfig(overall_threshold=7.5, coherence_threshold=7.0),
        ),
        analysis=analysis,
    )

    assert "overall" in instruction
    assert "coherence" in instruction
    assert "加强冲突" in instruction
    assert "人物动机" in instruction


def test_is_pause_or_cancel_requested_checks_status_and_cancel_flag():
    assert is_pause_or_cancel_requested(SimpleNamespace(status="paused", cancel_requested=False)) is True
    assert is_pause_or_cancel_requested(SimpleNamespace(status="cancelled", cancel_requested=False)) is True
    assert is_pause_or_cancel_requested(SimpleNamespace(status="running", cancel_requested=True)) is True
    assert is_pause_or_cancel_requested(SimpleNamespace(status="running", cancel_requested=False)) is False


def test_chapter_title_for_number_rewrites_stale_number_prefix():
    assert _chapter_title_for_number("第1章：开端", 12) == "第12章：开端"
    assert _chapter_title_for_number("转折", 5) == "第5章：转折"
