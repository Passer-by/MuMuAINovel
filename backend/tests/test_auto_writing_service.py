import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.auto_writing_service import (
    AutoWritingPolicy,
    QualityGateConfig,
    QualityGateResult,
    QualityScores,
    build_auto_writing_report,
    build_auto_writing_planning_prompt,
    build_consistency_guard_instruction,
    build_enhanced_quality_failure_record,
    build_model_attempt_sequence,
    build_volume_planning_instruction,
    format_project_chapters_for_export,
    safe_export_path_segment,
    build_seed_chapter_plans_from_idea,
    build_quality_failure_record,
    build_quality_retry_instruction,
    calculate_seed_chapter_count,
    extract_auto_writing_plan_from_ai_response,
    _chapter_title_for_number,
    evaluate_quality_gate,
    is_pause_or_cancel_requested,
    normalize_auto_writing_policy,
    select_chapters_for_batch,
    should_continue_writing,
    should_pause_for_auto_writing_failure,
    should_pause_for_quality_failures,
    validate_generated_chapter_quality,
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
        SimpleNamespace(id="4", chapter_number=4, status="skipped", content=""),
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
    assert request.automation_policy.auto_expand_outline is True
    assert request.automation_policy.failure_strategy == "repair_and_continue"


def test_start_request_accepts_automation_policy_config():
    request = AutoWritingStartRequest.model_validate(
        {
            "mode": "existing_project",
            "project_id": "project-1",
            "target_total_words": 50000,
            "chapters_per_batch": 3,
            "target_words_per_chapter": 3000,
            "automation_policy": {
                "auto_expand_outline": False,
                "auto_recover": False,
                "failure_strategy": "skip_chapter",
                "max_operation_retries": 5,
                "retry_backoff_seconds": 30,
                "min_word_ratio": 0.9,
                "repetition_check_chars": 1200,
                "max_repetition_ratio": 0.4,
                "require_chapter_hook": True,
                "consistency_check_enabled": False,
                "volume_planning_enabled": False,
                "auto_export_enabled": True,
                "budget_token_limit": 200000,
                "fallback_models": "model-a,model-b",
            },
        }
    )

    assert request.automation_policy.auto_expand_outline is False
    assert request.automation_policy.auto_recover is False
    assert request.automation_policy.failure_strategy == "skip_chapter"
    assert request.automation_policy.max_operation_retries == 5
    assert request.automation_policy.retry_backoff_seconds == 30
    assert request.automation_policy.min_word_ratio == 0.9
    assert request.automation_policy.repetition_check_chars == 1200
    assert request.automation_policy.max_repetition_ratio == 0.4
    assert request.automation_policy.require_chapter_hook is True
    assert request.automation_policy.consistency_check_enabled is False
    assert request.automation_policy.volume_planning_enabled is False
    assert request.automation_policy.auto_export_enabled is True
    assert request.automation_policy.budget_token_limit == 200000
    assert request.automation_policy.fallback_models == "model-a,model-b"


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


def test_normalize_auto_writing_policy_accepts_frontend_full_auto_options():
    policy = normalize_auto_writing_policy(
        {
            "auto_expand_outline": True,
            "auto_recover": True,
            "failure_strategy": "repair_and_continue",
            "max_operation_retries": 4,
            "retry_backoff_seconds": 12,
            "min_word_ratio": 0.85,
            "repetition_check_chars": 600,
            "max_repetition_ratio": 0.35,
            "require_chapter_hook": True,
            "consistency_check_enabled": True,
            "volume_planning_enabled": True,
            "auto_export_enabled": True,
            "budget_token_limit": 120000,
            "fallback_models": "gpt-5.2,gpt-5.1",
        }
    )

    assert policy == AutoWritingPolicy(
        auto_expand_outline=True,
        auto_recover=True,
        failure_strategy="repair_and_continue",
        max_operation_retries=4,
        retry_backoff_seconds=12,
        min_word_ratio=0.85,
        repetition_check_chars=600,
        max_repetition_ratio=0.35,
        require_chapter_hook=True,
        consistency_check_enabled=True,
        volume_planning_enabled=True,
        auto_export_enabled=True,
        budget_token_limit=120000,
        fallback_models=["gpt-5.2", "gpt-5.1"],
    )


def test_validate_generated_chapter_quality_reports_word_count_repetition_and_hook_failures():
    content = "同一句话循环叙述。" * 80

    result = validate_generated_chapter_quality(
        content=content,
        target_word_count=2000,
        policy=AutoWritingPolicy(
            min_word_ratio=0.8,
            repetition_check_chars=200,
            max_repetition_ratio=0.2,
            require_chapter_hook=True,
        ),
        previous_content_tail="同一句话循环叙述。" * 40,
    )

    assert result.passed is False
    assert "word_count" in result.failing_checks
    assert "repetition" in result.failing_checks
    assert "chapter_hook" in result.failing_checks


def test_validate_generated_chapter_quality_passes_when_constraints_are_met():
    content = (
        "他推开门，看见灯下未拆封的信。"
        "风从走廊尽头涌来，把墙上的旧照片吹得哗哗作响。"
        "她没有立刻回答，只把那枚刻着编号的钥匙放进他掌心。"
        "直到楼下传来急促的脚步声，两人才意识到真正的追兵已经到了。"
        "下一秒，门外有人轻轻敲了三下。"
    )

    result = validate_generated_chapter_quality(
        content=content,
        target_word_count=40,
        policy=AutoWritingPolicy(
            min_word_ratio=0.8,
            repetition_check_chars=120,
            max_repetition_ratio=0.75,
            require_chapter_hook=True,
        ),
        previous_content_tail="旧城在雨里沉默，没人知道钥匙去了哪里。",
    )

    assert result.passed is True
    assert result.failing_checks == []


def test_should_pause_for_auto_writing_failure_respects_strategy():
    assert should_pause_for_auto_writing_failure(2, AutoWritingPolicy(failure_strategy="pause", max_operation_retries=3)) is True
    assert should_pause_for_auto_writing_failure(2, AutoWritingPolicy(failure_strategy="skip_chapter", max_operation_retries=3)) is False
    assert should_pause_for_auto_writing_failure(4, AutoWritingPolicy(failure_strategy="repair_and_continue", max_operation_retries=3)) is True
    assert should_pause_for_auto_writing_failure(1, AutoWritingPolicy(failure_strategy="fail", max_operation_retries=3)) is False


def test_build_enhanced_quality_failure_record_includes_non_score_checks():
    record = build_enhanced_quality_failure_record(
        chapter_id="chapter-1",
        chapter_number=3,
        scores=QualityScores(overall=8.0, coherence=8.0, pacing=8.0, engagement=8.0),
        gate_result=evaluate_quality_gate(
            QualityScores(overall=8.0, coherence=8.0, pacing=8.0, engagement=8.0),
            QualityGateConfig(overall_threshold=7.0, coherence_threshold=7.0),
        ),
        retry_count=2,
        validation_failures=["word_count", "chapter_hook"],
        action="repair_and_continue",
    )

    assert record["failing_scores"] == []
    assert record["validation_failures"] == ["word_count", "chapter_hook"]
    assert record["action"] == "repair_and_continue"


def test_build_consistency_guard_instruction_summarizes_recent_state():
    instruction = build_consistency_guard_instruction(
        {
            "recent_chapters": [
                {"chapter_number": 4, "title": "旧钥匙", "summary": "主角拿到钥匙"},
                {"chapter_number": 5, "title": "暗门", "summary": "暗门开启，追兵出现"},
            ],
            "open_threads": ["钥匙来源未知", "追兵身份未揭露"],
            "character_states": {"林澈": "受伤但仍持有钥匙"},
        }
    )

    assert "第4章《旧钥匙》" in instruction
    assert "钥匙来源未知" in instruction
    assert "林澈：受伤但仍持有钥匙" in instruction


def test_build_quality_retry_instruction_includes_hard_validation_failures():
    instruction = build_quality_retry_instruction(
        scores=QualityScores(overall=8.0, coherence=8.0, pacing=8.0, engagement=8.0),
        gate_result=QualityGateResult(passed=True, failing_scores=[]),
        analysis=None,
        validation_result=validate_generated_chapter_quality(
            content="短章",
            target_word_count=1000,
            policy=AutoWritingPolicy(min_word_ratio=0.8, require_chapter_hook=True),
        ),
        consistency_instruction="【长篇一致性约束】角色仍持有钥匙。",
    )

    assert "word_count" in instruction
    assert "chapter_hook" in instruction
    assert "角色仍持有钥匙" in instruction


def test_build_volume_planning_instruction_describes_story_phase():
    instruction = build_volume_planning_instruction(
        generated_chapters=30,
        target_total_words=100000,
        current_words=60000,
    )

    assert "后段转折" in instruction
    assert "第 31 章" in instruction


def test_build_auto_writing_report_collects_run_metrics():
    report = build_auto_writing_report(
        generated_chapters=8,
        quality_failures=[{"chapter_number": 2}, {"chapter_number": 5}],
        policy=AutoWritingPolicy(auto_recover=True, failure_strategy="repair_and_continue"),
        current_words=24000,
        target_total_words=30000,
        token_usage={"estimated_total": 100000},
    )

    assert report["generated_chapters"] == 8
    assert report["quality_failure_count"] == 2
    assert report["auto_recover"] is True
    assert report["completion_ratio"] == 0.8
    assert report["token_usage"]["estimated_total"] == 100000


def test_build_model_attempt_sequence_uses_primary_then_unique_fallbacks():
    assert build_model_attempt_sequence("gpt-main", ["gpt-a", "gpt-main", " gpt-b "]) == [
        "gpt-main",
        "gpt-a",
        "gpt-b",
    ]
    assert build_model_attempt_sequence(None, ["gpt-a", "gpt-a"]) == ["gpt-a"]
    assert build_model_attempt_sequence(None, []) == [None]


def test_format_project_chapters_for_export_matches_txt_import_format():
    project = SimpleNamespace(title="星海旧约")
    chapters = [
        SimpleNamespace(chapter_number=1, title="失落信标", content="第一段\n\n第二段"),
        SimpleNamespace(chapter_number=2, title="", content=""),
    ]

    exported = format_project_chapters_for_export(project, chapters)

    assert exported["filename"] == "星海旧约.txt"
    assert "第1章 失落信标" in exported["content"]
    assert "　　第一段" in exported["content"]
    assert "第2章 未命名章节2" in exported["content"]
    assert "　　（本章暂无内容）" in exported["content"]


def test_safe_export_path_segment_blocks_path_escape_segments():
    assert safe_export_path_segment("local-user_123") == "local-user_123"
    assert safe_export_path_segment("../secret") == "___secret"
    assert safe_export_path_segment("..") == "__"
    assert safe_export_path_segment("") == "unknown"
