<!-- PROMPT: empty_result_diagnosis
     PLACEHOLDERS: {question}, {sql}, {column_statistics}
     USED BY: nodes/execution.py -> make_empty_result_check (in-graph gate)
              routes/insights.py -> /api/empty-result-hint (async client hint)
     PURPOSE: Decide whether a 0-row result plausibly answers the question, or
              whether the SQL is likely wrong (an INNER JOIN on a nullable
              dimension dropping rows, or an over-restrictive filter). Also
              return a one-line, user-facing likely-cause hint.
-->

A SQL query ran successfully but returned ZERO rows. Decide whether that is a plausible real answer, or whether the query is likely wrong.

Question:
{question}

SQL that returned no rows:
{sql}

Column statistics (nullability / null ratios of the involved columns; a column shown as `nulls N%` or without `NOT NULL` can be NULL/empty for some rows):
{column_statistics}

Guidance:
- An aggregate such as "total/count/sum by <dimension>" over a fact table that has data should NOT be empty. If it is, the likely cause is an INNER JOIN to a dimension that is NULL/empty for some rows (use LEFT JOIN), a wrong join key, or an over-restrictive WHERE the user did not ask for.
- Zero rows IS plausible when the question genuinely restricts the data (an explicit filter, a specific entity, a future/empty date range, "customers who never bought", etc.).

Respond with ONLY a compact JSON object, no prose and no markdown fences:
{{"plausible": true|false, "reason": "<=1 short sentence explaining why it is or isn't plausible>", "hint": "<=1 short, friendly sentence telling the user the most likely reason there are no records>"}}
