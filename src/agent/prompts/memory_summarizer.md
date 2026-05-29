<!-- PROMPT: memory_summarizer
     PLACEHOLDERS: {conversation_history}
     USED BY: nodes/memory.py -> make_memory_summarizer
     PURPOSE: Condense conversation history into a short summary when the token
              budget is exceeded. The output is injected into fused_router as
              the conversation_summary value.
-->

Summarize the following data analytics conversation concisely in at most 150 words.

Focus on:
- What data topics or questions were explored
- Key results or metrics discovered
- Any follow-up context that would be useful for the next query

Do NOT include SQL code in the summary. Use plain English only.

---

**Conversation to summarize:**
{conversation_history}
