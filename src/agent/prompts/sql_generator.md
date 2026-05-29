<!-- PROMPT: sql_generator
     PLACEHOLDERS: {question}, {error_context}, {retry_count}
     USED BY: nodes/sql_gen.py → make_sql_generator
     PURPOSE: User message injected ONLY on retry attempts (retry_count > 0).
              Feeds the structured error context back to the LLM so it can
              generate a corrected SQL query.
              On the first attempt, the raw question is used directly.
-->

The previous SQL attempt (attempt #{retry_count}) failed with the following error:

---
{error_context}
---

Please carefully analyze the error and generate a **corrected** SQL query for the original question below.

If the error mentions an unknown table, choose the closest matching table from the catalog.
If it is a syntax error, fix only the problematic part and preserve the intent.
If the query result did not answer the question, rethink the approach entirely.

**Original question:** {question}
