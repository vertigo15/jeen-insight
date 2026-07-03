# Agent State Flow

LangGraph state graph for the Jeen Insights text-to-SQL agent.
Every query passes through this graph from `START` to `END`.

> **Full end-to-end path** (UI → Flask → API → pre-graph → LangGraph → insights/charts):
> see [question-to-answer-flow.md](./question-to-answer-flow.md).

## Diagram

```mermaid
flowchart LR
    S([START])
    E([END])

    subgraph Memory["Memory"]
        MSC["memory_shrink_check\nlogic"]
        MS["memory_summarizer\nLLM"]
        MAG["memory_answer_generator\nLLM"]
    end

    subgraph Routing["Routing"]
        FR["fused_router\nLLM"]
    end

    subgraph CatalogPrompt["Catalog + Prompt"]
        CL["catalog_lookup\nDB or MCP"]
        PB["prompt_builder\nlogic"]
    end

    subgraph SqlSafety["SQL + Safety"]
        SG["sql_generator\nLLM"]
        SV["sqlglot_validate\nlogic"]
        DC["dlp_check\nlogic"]
    end

    subgraph ExecutionEval["Execution + Eval"]
        EQ["execute_query\nDB"]
        TRC["trivial_result_check\nlogic"]
        FEA["fused_eval_analytics\nLLM"]
        FC["feedback_classifier\nlogic"]
    end

    subgraph Output["Output"]
        RF["response_formatter\nlogic"]
        STM["save_to_memory\nDB"]
        OL["observability_log\nlogic"]
    end

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
```

## Parallel vs Logical Branches

The columns above are logical groupings, not simultaneous LangGraph execution. At runtime the graph follows one route through alternatives such as memory answer, normal SQL generation, blocked request, retry, or terminal formatting.

The true parallel work happens before `START`: `JeenInsightsAgent.process_question()` uses `asyncio.gather()` to resolve the user, load short-term memory, create the audit row, and preload catalog metadata. Once `graph.ainvoke()` starts, trace events are emitted in node execution order.

## Node Reference

| Icon | Node | Type | Description |
|------|------|------|-------------|
| 🧠 | `memory_shrink_check` | Logic | Checks whether conversation history exceeds the token budget |
| 🤏 | `memory_summarizer` | LLM | Condenses history into a short summary when over budget |
| 🔀 | `fused_router` | LLM | Classifies the question: `needs_query`, `from_memory`, `out_of_scope`, `unsafe`, `greeting` |
| 💬 | `memory_answer_generator` | LLM | Answers directly from conversation history; escapes to SQL path if needed |
| 📦 | `catalog_lookup` | DB/MCP | Loads table/column metadata from the active catalog source |
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
