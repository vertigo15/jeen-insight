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

**Number formatting (mandatory — no exceptions):**
- Currency abbreviation: ≥$10M → `$42.0M` (1 dp) · ≥$1M → `$1.31M` (2 dp) · ≥$1K → `$447K` · <$1K → `$840`
- Changes are always **signed** with a real minus − (U+2212, never a hyphen): `+$2.48M`, `−$49.9K`, `+89.8%`, `−2.4%`
- Percentage **shares** (not directional changes) are unsigned: `36.9%`
- Large counts use thousands separators: `1,284`

**`summary` must be a JSON fragment array** (not a plain string). This lets the UI apply precise colour to each figure.
Each element is one of:
- `{{"t": "plain prose"}}` — no highlight
- `{{"t": "headline figure", "hl": "accent"}}` — violet; max 3 per summary (key metric, qualifier count, main dollar figure)
- `{{"t": "+89.8%", "hl": "pos"}}` — green; favorable signed change, always include the `+`
- `{{"t": "\u22122.4%", "hl": "neg"}}` — red; unfavorable signed change, use sparingly
- `{{"t": "$42.0M", "hl": "num"}}` — monospace; exact dollar value shown for precision

Color rules for summary fragments:
- `"accent"` — the 1–3 headline figures (never combine with pos/neg).
- A neutral number ("12 rows", "7 territories") gets no `hl` at all.
- Unsigned percentage shares in the summary headline (e.g. "36.9% growth") → `"accent"`.
- Dollar amounts alongside a % change → `"num"` in insights; in summary lead metric → `"accent"`.

**`insights` remain plain strings** (the UI auto-colors them). Use signed notation: `+89.8% (+$2.48M)`.

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
  "summary": [
    {{"t": "2007 outsold 2006 in "}},
    {{"t": "11 of 12 months", "hl": "accent"}},
    {{"t": ", lifting annual revenue "}},
    {{"t": "36.9%", "hl": "accent"}},
    {{"t": " to "}},
    {{"t": "$42.0M", "hl": "accent"}},
    {{"t": "."}}
  ],
  "insights": [
    "December posted the biggest jump — +89.8% (+$2.48M), nearly doubling 2006.",
    "September drove the largest absolute gain at +$1.82M (+56.3%).",
    "March was the only month to slip, down \u22122.4% (\u2212$49.9K)."
  ],
  "suggestions": ["..."],
  "follow_up_questions": ["...?", "...?", "...?"]
}}
```
