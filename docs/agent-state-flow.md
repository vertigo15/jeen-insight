# Agent State Flow

LangGraph state graph for the Jeen Insights text-to-SQL agent.
Every query passes through this graph from `START` to `END`.

> **Full end-to-end path** (UI → Flask → API → pre-graph → LangGraph → insights/charts):
> see [question-to-answer-flow.md](./question-to-answer-flow.md).

## Diagram

```mermaid
flowchart TD
    S([▶ START])
    E([⏹ END])

    MSC["🧠 memory_shrink_check\n― logic ―"]
    MS["🤏 memory_summarizer\n― LLM ―"]
    FR["🔀 fused_router\n― LLM ―"]
    MAG["💬 memory_answer_generator\n― LLM ―"]
    CL["📦 catalog_lookup\n― DB ―"]
    PB["🔧 prompt_builder\n― logic ―"]
    SG["🧠 sql_generator\n― LLM ―"]
    SV["✅ sqlglot_validate\n― logic ―"]
    DC["🛡️ dlp_check\n― logic ―"]
    EQ["▶ execute_query\n― DB ―"]
    TRC["⚡ trivial_result_check\n― logic ―"]
    FEA["📊 fused_eval_analytics\n― LLM ―"]
    FC["🔁 feedback_classifier\n― logic ―"]
    RF["📋 response_formatter\n― logic ―"]
    STM["💾 save_to_memory\n― DB ―"]
    OL["🪵 observability_log\n― logic ―"]

    S --> MSC
    MSC -->|over budget| MS
    MSC -->|within budget| FR
    MS --> FR

    FR -->|needs_query| CL
    FR -->|from_memory| MAG
    FR -->|out_of_scope / unsafe / greeting| RF

    MAG -->|answer ready| RF
    MAG -->|needs fresh data| CL

    CL --> PB --> SG

    SG -->|SQL generated| SV
    SG -->|clarification / empty| RF

    SV -->|valid| DC
    SV -->|syntax error| FC

    DC -->|safe| EQ
    DC -->|blocked| RF

    EQ -->|rows returned| TRC
    EQ -->|exec error| FC

    TRC -->|trivial or eval disabled| RF
    TRC -->|needs evaluation| FEA

    FEA -->|answers intent| RF
    FEA -->|wrong result| FC

    FC -->|exhausted retries| RF
    FC -->|missing table| CL
    FC -->|syntax / exec / semantic| SG

    RF --> STM --> OL --> E

    classDef llm    fill:#ede9fe,stroke:#7c3aed,color:#4c1d95,font-weight:bold
    classDef db     fill:#d1fae5,stroke:#059669,color:#064e3b,font-weight:bold
    classDef logic  fill:#f1f5f9,stroke:#64748b,color:#1e293b
    classDef term   fill:#0f172a,stroke:#0f172a,color:#f8fafc,font-weight:bold

    class MS,FR,MAG,SG,FEA llm
    class CL,EQ,STM db
    class MSC,PB,SV,DC,TRC,FC,RF,OL logic
    class S,E term
```

## Node Reference

| Icon | Node | Type | Description |
|------|------|------|-------------|
| 🧠 | `memory_shrink_check` | Logic | Checks whether conversation history exceeds the token budget |
| 🤏 | `memory_summarizer` | LLM | Condenses history into a short summary when over budget |
| 🔀 | `fused_router` | LLM | Classifies the question: `needs_query`, `from_memory`, `out_of_scope`, `unsafe`, `greeting` |
| 💬 | `memory_answer_generator` | LLM | Answers directly from conversation history; escapes to SQL path if needed |
| 📦 | `catalog_lookup` | DB | Loads table/column metadata from the metadata database |
| 🔧 | `prompt_builder` | Logic | Assembles the dynamic system prompt with injected metadata |
| 🧠 | `sql_generator` | LLM | Generates SQL or returns a clarification message |
| ✅ | `sqlglot_validate` | Logic | Parses and validates SQL syntax and table references via sqlglot |
| 🛡️ | `dlp_check` | Logic | Blocks governed columns or unsafe SQL patterns before execution |
| ▶ | `execute_query` | DB | Runs the SQL against PostgreSQL in a read-only transaction |
| ⚡ | `trivial_result_check` | Logic | Skips expensive eval for tiny/direct results |
| 📊 | `fused_eval_analytics` | LLM | Evaluates whether the result answers the intent; produces summary + insights |
| 🔁 | `feedback_classifier` | Logic | Routes errors back to `sql_generator` or `catalog_lookup`; gives up after max retries |
| 📋 | `response_formatter` | Logic | Formats the final API response payload |
| 💾 | `save_to_memory` | DB | Persists the query record and result to conversation history |
| 🪵 | `observability_log` | Logic | Logs route, retries, token counts, latency, and final status |

## Node Types

| Type | Color | Role |
|------|-------|------|
| **LLM** | Purple | Calls the language model — `memory_summarizer`, `fused_router`, `memory_answer_generator`, `sql_generator`, `fused_eval_analytics` |
| **DB** | Green | Reads from or writes to a database — `catalog_lookup`, `execute_query`, `save_to_memory` |
| **Logic** | Gray | Pure Python, no external I/O — all remaining nodes |

## Key Loops

- **Retry loop** — `feedback_classifier` routes back to `sql_generator` (syntax/exec/semantic errors)
  or to `catalog_lookup` (missing table) until retries are exhausted (default max: 3).
- **Memory escape** — `memory_answer_generator` can fall through to the full SQL path
  when conversation history is insufficient to answer.
- **Eval rejection** — `fused_eval_analytics` sends the result back to `feedback_classifier`
  when the SQL result does not satisfy the original user intent.

## Source

Graph definition: `src/agent/langgraph_agent/graph.py`
Node implementations: `src/agent/langgraph_agent/nodes/`
