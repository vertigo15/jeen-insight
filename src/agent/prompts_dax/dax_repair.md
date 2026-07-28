<!-- PROMPT: dax_repair
     PLACEHOLDERS: {question}, {dax}, {error_context}, {lint_errors}, {plan}
     USED BY: langgraph_agent_dax/nodes/dax_validate.py -> dax_repair
     PURPOSE: Focused local repair of a DAX query that failed static validation
              (lexer/linter/symbol resolution) BEFORE it is ever executed.
              str.format template: literal curly braces are doubled ({{ }}).
-->

The DAX query below failed static validation before execution. Fix ONLY what the errors call out; keep the query's intent and the plan intact.

# Failing DAX

{dax}

# Validation errors

{lint_errors}

# Additional context

{error_context}

# Plan to honor

{plan}

# Fix rules

- Keep it a single read-only query: one `EVALUATE` returning one table, optional `DEFINE` with `MEASURE`/`VAR` only.
- A measure is `[Name]` (no table). A column is `'Table'[Column]` (single-quoted table). Do not confuse the two.
- Only reference measures/columns/tables that exist in the plan/catalog. If a symbol is unknown, replace it with the closest valid one.
- Balance every `(`, `[`, `{{`, and quote. Remove any banned MDX/DMV/DDL tokens.
- If detail grain, ensure a bounded `TOPN` and an explicit `ORDER BY` are present.

Respond with exactly one `run_dax` tool call containing the corrected DAX. No prose, no fences.

**Original question:** {question}
