<!-- PROMPT: prior_data_binder
     PLACEHOLDERS: {question}, {conversation_history}, {prior_data}, {tables}, {columns}
     USED BY: nodes/binder.py -> make_prior_data_binder
     PURPOSE: When a new question builds on a prior result, extract the needed
              values from the stored rows (one PostgreSQL SELECT) and name the live
              catalog column those values filter. The app turns the values into a
              verified WHERE … IN (…) filter for the SQL generator.
-->

You prepare a database query that builds on an EARLIER result of this conversation.

**Conversation ledger:**
{conversation_history}

**Stored data of the referenced turn(s)** — loaded as PostgreSQL tables ({tables}); only column names and sample rows are shown, your SELECT runs over all stored rows:
{prior_data}

**Live catalog columns** (`table.column - Type: …`), the new query will run against these:
{columns}

**User question:**
{question}

---

Decide which VALUES from the stored data the new query must use, and which live column they filter.

Respond with JSON only:

```json
{{"extract_sql": "SELECT \"product_id\" FROM insights_mem_t3 ORDER BY \"price\" DESC LIMIT 4", "bind": {{"table": "dimproduct", "column": "productkey"}}, "reason": "<one sentence>"}}
```

Rules:
- `extract_sql` is ONE PostgreSQL `SELECT` over the listed tables only, returning a single column whose values identify the rows the user means (a key or a name). Use `ORDER BY … LIMIT n` for "top / most / largest N". Quote the exact column names shown with double quotes.
- `bind.table` / `bind.column` must be an existing live catalog column (from the list above) that holds the same kind of value, so that `WHERE table.column IN (values)` selects those rows in the new query. Prefer a key column when the stored data has one.
- If the question does not actually need values from the stored data (it only refers to the prior turn for context), or no live column matches, respond `{{"skip": true, "reason": "<why>"}}`.
