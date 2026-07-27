<!-- PROMPT: dax_generator
     PLACEHOLDERS: {question}, {error_context}, {retry_count},
                   {connection_display_name}, {dataset_id}, {plan}
     USED BY: langgraph_agent_dax/nodes/dax_gen.py -> dax_generator
     PURPOSE: User message injected ONLY on retry attempts (retry_count > 0),
              feeding the structured error context back to the model so it can
              generate a corrected DAX query. First attempt uses the raw question.
-->

The previous DAX attempt (attempt #{retry_count}) failed with the following error:

---
{error_context}
---

Carefully analyze the error and generate a **corrected** DAX query for the original question below.

Target Power BI dataset:
- Connection: {connection_display_name}
- Dataset id: {dataset_id}

Follow the typed plan (do not change its measures/grain/filters unless the error requires it):

{plan}

Respond with exactly one `run_dax` tool call containing a single read-only DAX query (one `EVALUATE`, one table, optional `DEFINE` with `MEASURE`/`VAR` only).
Do not include DAX in the message body, Markdown fences, explanations before the tool call, or a trailing semicolon.

Repair guidance by error kind:
- Unknown measure/column: pick the closest match from the plan/catalog; a measure is `[Name]`, a column is `'Table'[Column]`.
- "Column used as a value" / needs aggregation: wrap the column in `SUM`/`AVERAGE`/`COUNTROWS`, or in `CALCULATE(...)` for context transition.
- More than one result table / multiple EVALUATE: collapse to a single `EVALUATE` returning one table.
- Too many rows / resource limit: switch to an aggregate, or tighten `TOPN` and add filters.
- Relationship/ambiguous path: use `SUMMARIZECOLUMNS` (auto relationships) or `USERELATIONSHIP` inside `CALCULATE`.

**Original question:** {question}
