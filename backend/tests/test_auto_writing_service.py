import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.auto_writing_service import (
    QualityGateConfig,
    QualityScores,
    build_quality_failure_record,
    evaluate_quality_gate,
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
