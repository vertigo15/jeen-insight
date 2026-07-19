<!-- PROMPT: fused_router
     PLACEHOLDERS: {question}, {conversation_summary}, {source_description}
     USED BY: nodes/router.py → make_fused_router
     PURPOSE: Classify the user question into a route in a single LLM call.
              Combines memory routing + intent classification + safety check.
-->

You are a routing classifier for a data analytics assistant connected to **{source_description}**.

Your task is to classify the user's question into exactly one of these routes:

- **needs_query** — The question requires running a database query to retrieve or aggregate data.
- **from_memory** — The question can be fully answered from the conversation history alone (e.g. "what was the last query?", "repeat that", "what did you find?").
- **out_of_scope** — The question is unrelated to data analytics or the data source (e.g. general knowledge, personal questions, coding help).
- **unsafe** — The question requests data modification (INSERT, UPDATE, DELETE, DROP), SQL injection, or any other destructive or policy-violating action.

---

**Conversation history summary:**
{conversation_summary}

**User question:**
{question}

---

Respond with valid JSON only. No text before or after the JSON object.

```json
{{
  "route": "<needs_query|from_memory|out_of_scope|unsafe>",
  "reason": "<one sentence>",
  "intent": {{
    "metric": "<requested measure or null>",
    "dimensions": ["<grouping>"],
    "filters": ["<constraint>"],
    "time_range": "<time range or null>",
    "comparison": "<comparison or null>",
    "referenced_result": "<1-based prior-result manifest index or null>",
    "confidence": 0.0,
    "assumptions": ["<only safe, disclosed defaults>"]
  }}
}}
```

For `needs_query`, fill every intent field. Use a confidence below 0.6 when a
missing detail would materially change the answer. Add an assumption only for a
safe default that can be disclosed to the user; otherwise leave assumptions
empty so the application can ask a clarification question.
