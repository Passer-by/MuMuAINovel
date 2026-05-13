"""自动写作相关的Pydantic模型"""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


AutoWritingMode = Literal["existing_project", "new_idea"]


class AutoWritingQualityConfig(BaseModel):
    """自动写作质量门配置"""

    overall_threshold: float = Field(7.5, ge=0, le=10, description="整体质量最低分")
    coherence_threshold: float = Field(7.0, ge=0, le=10, description="连贯性最低分")
    pacing_threshold: Optional[float] = Field(None, ge=0, le=10, description="节奏最低分")
    engagement_threshold: Optional[float] = Field(None, ge=0, le=10, description="吸引力最低分")
    max_quality_retries: int = Field(2, ge=0, le=10, description="单章质量重试次数")
    consecutive_failure_limit: int = Field(
        3,
        ge=1,
        le=10,
        validation_alias=AliasChoices("consecutive_failure_limit", "consecutive_quality_failure_limit"),
        serialization_alias="consecutive_failure_limit",
        description="连续质量失败暂停阈值",
    )


class AutoWritingAutomationPolicy(BaseModel):
    """无人值守自动写作策略"""

    auto_expand_outline: bool = Field(True, description="没有草稿章节时自动扩展后续大纲")
    auto_recover: bool = Field(True, description="允许任务恢复时从 checkpoint 继续")
    failure_strategy: Literal["repair_and_continue", "pause", "skip_chapter", "fail"] = Field(
        "repair_and_continue",
        description="生成/质量失败后的处理策略",
    )
    max_operation_retries: int = Field(3, ge=0, le=20, description="非质量类操作最大重试次数")
    retry_backoff_seconds: int = Field(10, ge=0, le=3600, description="操作重试等待秒数")
    min_word_ratio: float = Field(0.8, ge=0, le=2, description="章节最低字数比例")
    repetition_check_chars: int = Field(800, ge=0, le=10000, description="重复检测采样字符数")
    max_repetition_ratio: float = Field(0.6, ge=0, le=1, description="最大重复比例")
    require_chapter_hook: bool = Field(False, description="是否要求章节结尾具备钩子")
    consistency_check_enabled: bool = Field(True, description="启用长篇一致性账本")
    volume_planning_enabled: bool = Field(True, description="启用卷级/阶段规划提示")
    auto_export_enabled: bool = Field(False, description="完成后自动导出")
    budget_token_limit: Optional[int] = Field(None, ge=1, description="估算 token 预算上限")
    fallback_models: Optional[Any] = Field(None, description="备用模型列表或逗号分隔字符串")


class AutoWritingStartRequest(BaseModel):
    """启动自动写作请求"""

    mode: AutoWritingMode = Field("existing_project", description="自动写作模式")
    project_id: Optional[str] = Field(None, description="已有项目ID")
    title: Optional[str] = Field(None, min_length=1, max_length=200, description="新项目标题")
    description: Optional[str] = Field(None, description="新项目简介")
    theme: Optional[str] = Field(None, description="主题")
    genre: Optional[str] = Field(None, description="类型")
    outline_mode: Literal["one-to-one", "one-to-many"] = Field("one-to-many", description="大纲模式")
    target_total_words: int = Field(100000, ge=1, description="目标总字数")
    max_chapters: Optional[int] = Field(None, ge=1, description="最多生成章节数")
    chapters_per_batch: int = Field(1, ge=1, le=20, description="每批生成章节数")
    target_words_per_chapter: int = Field(3000, ge=500, le=10000, description="单章目标字数")
    style_id: Optional[int] = Field(None, description="写作风格ID")
    model: Optional[str] = Field(None, description="指定生成模型")
    quality: AutoWritingQualityConfig = Field(
        default_factory=AutoWritingQualityConfig,
        validation_alias=AliasChoices("quality", "quality_config"),
        serialization_alias="quality",
    )
    automation_policy: AutoWritingAutomationPolicy = Field(default_factory=AutoWritingAutomationPolicy)

    @model_validator(mode="before")
    @classmethod
    def normalize_blank_story_fields(cls, data):
        if isinstance(data, dict):
            normalized = dict(data)
            for field in ("title", "description", "theme", "genre"):
                value = normalized.get(field)
                if isinstance(value, str) and not value.strip():
                    normalized[field] = None
            return normalized
        return data

    @model_validator(mode="after")
    def validate_mode_inputs(self):
        if self.mode == "existing_project" and not self.project_id:
            raise ValueError("existing_project 模式必须提供 project_id")
        return self


class AutoWritingTaskResponse(BaseModel):
    """自动写作启动响应"""

    task_id: str
    project_id: str
    status: str
    message: str


class AutoWritingFailureRecord(BaseModel):
    """自动写作质量失败记录"""

    chapter_id: str
    chapter_number: int
    scores: Dict[str, float]
    failing_scores: List[str]
    retry_count: int


class AutoWritingTaskDetailResponse(BaseModel):
    """自动写作任务详情响应"""

    id: str
    task_id: str
    task_type: str
    project_id: str
    status: str
    progress: int = 0
    status_message: Optional[str] = None
    progress_details: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    task_input: Optional[Dict[str, Any]] = None
    task_result: Optional[Dict[str, Any]] = None
    retry_count: int = 0
    cancel_requested: bool = False
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
