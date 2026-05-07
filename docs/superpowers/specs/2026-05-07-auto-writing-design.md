# Auto Writing Design

## Context

MuMuAINovel already has most of the parts needed for semi-automatic novel writing:

- Wizard endpoints generate world building, career systems, characters, and initial outlines.
- Outline endpoints can generate or continue outlines, and expand outlines into chapter plans.
- Chapter endpoints can generate one chapter, run ordered batch generation, analyze chapters, and regenerate chapters from analysis suggestions.
- Background tasks already provide polling, cancellation, per-user queues, and persisted progress.
- Chapter analysis stores quality scores in `plot_analysis`: overall quality, pacing, engagement, coherence, and suggestions.

The current gap is orchestration. Users still have to move between screens and trigger each step manually. The new feature should introduce a single automatic writing workflow that can start from an existing project or from a new idea, keep working in batches, retry low-quality chapters, and reduce manual intervention.

## Goals

1. Support automatic writing for existing projects.
2. Support automatic writing from a new idea or inspiration.
3. Let users configure batch size at startup instead of hard-coding it.
4. Use chapter analysis scores as a quality gate.
5. Automatically rewrite low-scoring chapters from analysis suggestions.
6. Continue after isolated quality failures, but pause after repeated quality failures.
7. Preserve task progress so long-running work can be monitored, paused, resumed, or cancelled.

## Non-Goals

- Do not build a fully autonomous agent that invents arbitrary next actions without a deterministic workflow.
- Do not replace existing wizard, outline, chapter generation, or analysis logic.
- Do not force a single global quality threshold for all users.
- Do not make automatic writing run as an unbounded process without user-provided limits.

## Recommended Approach

Add a dedicated automatic writing module:

- `AutoWritingTask` model for persisted orchestration state.
- `AutoWritingService` for deterministic workflow execution.
- `auto_writing` API routes for starting, pausing, resuming, cancelling, and inspecting tasks.
- Frontend automatic writing entry points for existing projects and idea-based creation.

This keeps orchestration separate from large existing route files such as `chapters.py` and `outlines.py`, while reusing their core services and helper logic where practical.

## User Workflows

### Existing Project

The user selects an existing project and starts automatic writing with startup configuration:

- target total words
- chapters per batch
- outline nodes per batch or continuation count
- chapters per outline for one-to-many projects
- target words per chapter
- writing style
- model
- quality thresholds
- max quality retries per chapter
- consecutive quality failure pause threshold

The service inspects project state and resumes from the earliest useful step:

1. If the project has incomplete wizard data, skip only the already completed data and generate missing setup data where possible.
2. If there are outlines without chapters in one-to-many mode, expand them.
3. If there are draft chapters without content, generate them in chapter order.
4. If completed chapters lack analysis, analyze them.
5. If analysis scores fail the quality gate, retry the chapter within configured limits.
6. If the project has not reached target words, continue outlines, expand, and generate the next batch.
7. Stop when target words or max chapter limits are reached.

### New Idea

The user starts from a short idea and configuration:

- title or working title
- genre
- theme or inspiration text
- target total words
- narrative perspective
- character count
- outline mode
- batch settings
- quality settings
- writing style and model

The service creates a project, then runs:

1. world building
2. career system, when applicable to the project genre or enabled by the user
3. characters and organizations
4. initial outlines
5. outline expansion
6. chapter generation
7. chapter analysis
8. quality retry
9. continuation batches until limits are reached

## Startup Configuration

Create a schema such as `AutoWritingStartRequest`:

- `mode`: `existing_project` or `new_idea`
- `project_id`: required for existing projects
- `idea`: required for new idea mode
- `title`
- `genre`
- `theme`
- `target_total_words`
- `max_chapters`
- `chapters_per_batch`: user configured, recommended range `1-20`
- `outline_nodes_per_batch`: recommended range `1-20`
- `chapters_per_outline`: recommended range `1-10`
- `target_words_per_chapter`: existing range `500-10000`
- `style_id`
- `model`
- `narrative_perspective`
- `enable_career_system`
- `enable_mcp`
- `quality_overall_threshold`: default `7.5`
- `quality_coherence_threshold`: default `7.0`
- `quality_pacing_threshold`: optional
- `quality_engagement_threshold`: optional
- `max_quality_retries_per_chapter`: default `2`
- `consecutive_quality_failure_limit`: default `3`
- `failure_policy`: default `continue_until_consecutive_limit`

## Task State

Create `AutoWritingTask` with fields:

- `id`
- `user_id`
- `project_id`
- `status`: `pending`, `running`, `paused`, `completed`, `failed`, `cancelled`
- `stage`: `setup`, `world_building`, `career_system`, `characters`, `outline`, `expansion`, `chapter_generation`, `analysis`, `quality_retry`, `continuation`, `completed`
- `progress`
- `status_message`
- `config`
- `current_outline_id`
- `current_chapter_id`
- `current_chapter_number`
- `current_batch_index`
- `generated_chapters_count`
- `generated_words_count`
- `consecutive_quality_failures`
- `quality_failures`
- `stage_result`
- `error_message`
- `pause_requested`
- `cancel_requested`
- timestamps

`quality_failures` should store chapter-level records:

- chapter id and number
- attempt count
- final scores
- failing score names
- suggestions used
- whether the task continued or paused

## Workflow Details

### Existing Project Inspection

The service should derive next work from database state instead of trusting only task state:

- Count current project words from chapters or use `Project.current_words` with reconciliation if needed.
- Find outlines with no chapters in one-to-many mode.
- Find draft chapters with no content.
- Find completed chapters without `PlotAnalysis`.
- Find chapters whose latest analysis does not satisfy quality thresholds and whose retry count is below the task limit.

This makes resume safer after server restarts.

### Batch Loop

Each batch should:

1. Ensure there are enough outline nodes for the batch.
2. Expand outline nodes into chapters when needed.
3. Generate chapters in strict chapter order.
4. Analyze each generated chapter.
5. Apply quality retry for each chapter before moving to the next chapter where possible.
6. Update project word count and task counters.
7. Decide whether another batch is needed.

### Quality Gate

After analysis completes, load `PlotAnalysis` for the chapter and evaluate:

- pass if `overall_quality_score >= quality_overall_threshold`
- pass if `coherence_score >= quality_coherence_threshold`
- pass optional pacing and engagement thresholds only when configured

If the chapter fails:

1. If retry count is below `max_quality_retries_per_chapter`, regenerate the chapter from all analysis suggestions and automatically apply the result.
2. Analyze the regenerated chapter again.
3. Repeat until pass or max retries.
4. If still failed, increment `consecutive_quality_failures`, add a quality failure record, and continue to the next chapter.
5. If `consecutive_quality_failures >= consecutive_quality_failure_limit`, pause the task with a clear status message.

When a chapter passes, reset `consecutive_quality_failures` to zero.

### Regeneration

Prefer reusing the existing chapter regeneration path conceptually:

- `modification_source`: `analysis_suggestions`
- `selected_suggestion_indices`: all available suggestions
- `auto_apply`: true
- `target_word_count`: current task target chapter words
- `style_id`: task style
- preserve character traits by default

If the current regeneration implementation is SSE-oriented, extract the reusable generation logic into a service method before wiring it into `AutoWritingService`.

### Pause, Resume, Cancel

- Pause should set `pause_requested` and let the current atomic substep finish.
- Resume should clear `pause_requested`, set status to `pending`, and enqueue execution.
- Cancel should set `cancel_requested`; generation and analysis loops should check it between substeps and during streaming chunks where available.
- Server restart recovery can be handled by a later scheduler pass, but the task design should not prevent it.

## API Design

Add routes under `/auto-writing`:

- `POST /auto-writing/start` starts a new automatic writing task.
- `GET /auto-writing/{task_id}` returns task status and details.
- `GET /auto-writing/project/{project_id}/tasks` lists project auto-writing tasks.
- `POST /auto-writing/{task_id}/pause` requests pause.
- `POST /auto-writing/{task_id}/resume` resumes a paused or failed-recoverable task.
- `POST /auto-writing/{task_id}/cancel` cancels the task.
- `GET /auto-writing/{task_id}/quality-failures` returns low-quality chapter details.

## Frontend Design

Add an automatic writing entry point in project pages and project creation flows:

- Existing project button: "自动写作"
- New idea entry: "从灵感自动写"
- Startup modal or page with grouped settings:
  - story setup
  - generation batch
  - model and style
  - quality retry
  - stop conditions

Task panel should show:

- current status and stage
- current batch
- current chapter
- generated words and target words
- quality retry attempts
- low-quality chapters
- pause, resume, cancel actions

For failed quality chapters, show scores and suggestions so users can inspect or manually regenerate later.

## Data Consistency

- Generation, analysis, and quality retry must run sequentially per task to preserve story continuity.
- Existing per-user background queue behavior should be respected to avoid conflicting long-running tasks.
- Do not run two active automatic writing tasks for the same project at once.
- Batch generation should not skip chapter prerequisites.
- Quality retry should update the chapter content before the next chapter uses it as context.

## Testing Strategy

Backend tests:

- start task validation for existing project and new idea modes
- quality gate pass and fail decisions
- retry count behavior
- consecutive failure pause behavior
- resume after pause chooses the next correct substep
- duplicate active task prevention

Service-level tests with fakes:

- fake AI generation
- fake analysis scores
- fake regeneration that improves scores after retry
- fake regeneration that never improves scores and triggers pause after consecutive failures

Frontend tests:

- startup form validates required fields for both modes
- task panel renders stage, progress, and quality failure details
- pause, resume, and cancel call the right APIs

Manual verification:

- existing project with draft chapters can run one configured batch
- new idea can create a project and reach the first generated chapter
- low quality chapter retries automatically and records failure state when still below threshold

## Open Implementation Notes

- `chapters.py` is already large. New orchestration should live in new modules and extract shared logic only where necessary.
- `BatchGenerationTask` is chapter-generation-specific. Automatic writing should use its own task model rather than overloading it.
- Current analysis writes a single `PlotAnalysis` per chapter. Quality retry can rely on the latest updated record, but future version history may be useful.
- The automatic workflow can initially use polling, matching existing background task UX.
