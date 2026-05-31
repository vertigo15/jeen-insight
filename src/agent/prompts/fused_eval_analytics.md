<!-- PROMPT: fused_eval_analytics
     PLACEHOLDERS: {question}, {sql}, {results_sample}, {row_count}
     USED BY: nodes/eval.py → make_fused_eval_analytics
     PURPOSE: Single large-model call that evaluates whether the result answers
              the intent, then produces a summary, key insights, and a list of
              follow-up questions shown as clickable chips in the results card.
-->

You are a senior data analyst reviewing a query result.

**Original question:** {question}

**SQL executed:**
```sql
{sql}
```

**Row count:** {row_count}

**Sample results (first 5 rows):**
```json
{results_sample}
```

---

Your tasks:
1. **Evaluate** whether the result genuinely answers the original question.
2. **Summarize** what the data shows in 1–2 sentences for a business user. Focus on the headline numbers.
3. **Extract** 2–3 key insights or notable patterns, each backed by a specific number from the data.
4. **Suggest** 0–2 concrete, actionable next steps the user could take based on what the data shows.
5. **Generate** 3–4 short follow-up questions the user might want to ask next.

**Rules:**
- Set `answers_intent` to `false` ONLY when the result set is empty despite expecting data, or when the results clearly do not match what was asked.
- Set `answers_intent` to `true` for all other cases, including partial results.
- Keep `summary` under 60 words.
- Keep each `insights` item under 30 words and include at least one specific number.
- Each `suggestions` item is an actionable recommendation, ≤ 20 words, not a question.
- Each `follow_up_questions` item must be ≤ 15 words and end with `?`.
- `suggestions` and `follow_up_questions` may be empty lists if nothing natural applies.
- Match the language of the original question.

Respond with valid JSON only. No text before or after the JSON object.

```json
{{
  "answers_intent": true,
  "summary": "...",
  "insights": ["...", "...", "..."],
  "suggestions": ["..."],
  "follow_up_questions": ["...?", "...?", "...?"]
}}
```
