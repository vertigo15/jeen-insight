# Prompt locations — Jeen Insights

Every LLM prompt is a file. There is no inline prompt in Python code except
short static fallbacks.

| Directory | Used by |
|---|---|
| `src/agent/prompts/*.md` | The text-to-SQL graph, and the shared nodes of the DAX graph |
| `src/agent/prompts_dax/*.md` | The Power BI text-to-DAX graph only (served from disk) |
| `templates/insight_prompt.txt` | The legacy insights path (`src/agent/insight_service.py`) |

## Prompts in the Settings registry (editable live)

These are listed in `PROMPT_REGISTRY` in `src/api/routes/settings.py`. At API
startup `lifespan._seed_prompts` writes each one to the `insights_prompts` table:
new prompts are inserted, default rows are refreshed when the file changed, and
custom (edited) rows are never overwritten. Admins edit them in
**Settings → Prompts**, which keeps versions you can restore and an optional
per-prompt model.

| Name | File | Used for |
|---|---|---|
| `jeen_insights_system` | `jeen_insights_system.md` | Main system prompt: persona, SQL rules, how the catalog is presented. |
| `fused_router` | `fused_router.md` | `fused_router` node: classifies each message into a route. |
| `fused_eval_analytics` | `fused_eval_analytics.md` | `fused_eval_analytics` node: intent check, summary, findings, follow-ups. |
| `memory_answer` | `memory_answer.md` | `memory_answer_generator` node: follow-ups about a prior answer or its data. |
| `prior_data_binder` | `prior_data_binder.md` | `prior_data_binder` node: bind values from a prior result as a verified filter. |
| `sql_generator` | `sql_generator.md` | `sql_generator` retry message (not used on the first attempt). |
| `sql_repair` | `sql_repair.md` | `sql_repair` node: one focused edit of a failed statement. |
| `analysis_planner` | `analysis_planner.md` | `analysis_planner` node: choose an ML skill and fill its parameters. |
| `analysis_narration` | `analysis_narration.md` | Narration of an ML result, grounded in the engine's numbers. |
| `chart_editor` | `chart_editor.md` | Plain-language chart edits. |
| `chart_map_editor` | `chart_map_editor.md` | Plain-language edits of map charts. |
| `insights` | `templates/insight_prompt.txt` | Legacy insights prompt. |
| `autocomplete_suggestions` | `autocomplete_suggestions.md` | Question suggestions. |

## Prompts served from disk only

Not in the Settings registry; change the file and restart.

| File | Used by |
|---|---|
| `src/agent/prompts/capability_answer.md` | `capability_answer` ("what can you do?") on the SQL graph. |
| `src/agent/prompts/sql_filter_planner.md` | `filter_planner` node: bind literals to catalogued columns. |
| `src/agent/prompts/empty_result_diagnosis.md` | `empty_result_check` node. |
| `src/agent/prompts_dax/jeen_insights_system_dax.md` | DAX system prompt. |
| `src/agent/prompts_dax/dax_planner.md` | `dax_query_planner` node. |
| `src/agent/prompts_dax/dax_generator.md` | `dax_generator` node. |
| `src/agent/prompts_dax/dax_repair.md` | `dax_repair` node. |
| `src/agent/prompts_dax/capability_answer.md` | `capability_answer` on the DAX graph (Power BI wording). |

On the DAX graph a DAX file that shares a name with a base prompt wins and is
never replaced by the `insights_prompts` row of that name. Shared names still go
through the database, so a Settings edit applies to both graphs.

## Where to read more

- Which node calls which prompt, and the route map:
  [`docs/agent-state-flow.md`](docs/agent-state-flow.md) and
  [`docs/agent-state-flow-dax.md`](docs/agent-state-flow-dax.md).
- ML prompts and skills: [`docs/ml-skills.md`](docs/ml-skills.md).
- Prompt-management workflow: the "Prompt management" section of the
  [`README`](README.md).
