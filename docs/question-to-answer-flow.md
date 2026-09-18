# Question → Answer: Complete Flow

End-to-end path from a user typing a question in the browser to seeing results,
insights, and charts. Includes the Flask UI layer, FastAPI backend, LangGraph
agent, metadata catalog (DB or MCP), SQL tools, and optional follow-up LLM calls.

For LangGraph node details only, see also [agent-state-flow.md](./agent-state-flow.md).

**Draw.io:** Open [question-to-answer-flow.drawio](./question-to-answer-flow.drawio) in
[diagrams.net](https://app.diagrams.net/) or the Draw.io VS Code extension (9 pages).
Regenerate after edits with `python3 scripts/generate_question_flow_drawio.py`.

---

## 1. End-to-end overview

```mermaid
flowchart TB
    subgraph Browser["Browser (index.html + script.js)"]
        QIN["User types question"]
        ASK["POST /api/ask\n{question, connection, session_id, limit, temperature}"]
        DISP["displayResults()\nTable · SQL · Prompt · Trace"]
        INS["InsightsManager\nPOST /api/generate-insights"]
        CHART["ChartManager\nPOST /api/generate-chart"]
    end

    subgraph FlaskUI["Flask UI (ui_app.py)"]
        AUTH["Session auth\nuser_context from login"]
        PROXY["Proxy → FastAPI /api/query"]
    end

    subgraph FastAPI["FastAPI API (src/api)"]
        QUERY["POST /api/query"]
        AGENT["JeenInsightsAgent.process_question()"]
        INSAPI["POST /api/generate-insights"]
        CHARTAPI["POST /api/generate-chart"]
    end

    subgraph PreGraph["Pre-graph bootstrap (parallel)"]
        USER["SimpleUserResolver\n→ user_id"]
        CTX["ConversationHistory\nlast Q&As for session_id"]
        AUDIT["ConversationHistory.log_query\n→ query_id"]
        CAT0["Catalog preload\nMCP or metadata DB"]
    end

    subgraph LangGraph["LangGraph text-to-SQL graph"]
        LG["25 nodes: memory ledger → router → SQL / ML / memory → validate → execute → eval → format"]
    end

    subgraph DataSources["Data & catalog"]
        META["Metadata DB\nmetadata_* · knowledge_pairs"]
        MCP["MCP server\nlist_connections · get_catalog_prompt"]
        PG["User PostgreSQL\n(read-only SELECT)"]
        HIST["insights_conversation_sessions\naudit + memory"]
    end

    QIN --> ASK --> AUTH --> PROXY --> QUERY --> AGENT
    AGENT --> USER & CTX & AUDIT & CAT0
    AGENT --> LG
    LG --> META & MCP & PG & HIST
    LG --> DISP
    DISP --> INS --> INSAPI
    DISP --> CHART --> CHARTAPI
    INSAPI --> LG
    CHARTAPI --> LLM2["LLM (ECharts JSON)"]

    classDef ui fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef api fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef graph fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    classDef db fill:#d1fae5,stroke:#059669,color:#064e3b
    class QIN,ASK,DISP,INS,CHART ui
    class QUERY,AGENT,INSAPI,CHARTAPI,PROXY,AUTH api
    class LG graph
    class META,MCP,PG,HIST db
```

---

## 2. UI → API request path

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant UI as Browser script.js
    participant Flask as Flask UI :8501
    participant API as FastAPI :8000
    participant Agent as JeenInsightsAgent
    participant Graph as LangGraph

    User->>UI: Submit question (Enter / Ask)
    UI->>UI: requireConnection(), JeenPreferences
    UI->>Flask: POST /api/ask JSON
    Note over Flask: Injects user_context<br/>{user_id, user_name, user_email}<br/>from signed session cookie
    Flask->>API: POST /api/query
    API->>Agent: process_question(question, session_id, user_context, …)
    Agent->>Graph: ainvoke(initial_state)
    Graph-->>Agent: final_state + trace
    Agent-->>API: formatted_response
    API-->>Flask: QueryResponse JSON
    Flask-->>UI: {sql, results, query_id, session_id, trace, metrics, …}
    UI->>UI: displayResults() · renderTrace() · displayHistory()
    opt AI Analytics enabled
        UI->>Flask: POST /api/generate-insights
        Flask->>API: insights eval subgraph
    end
    opt User switches to Chart view
        UI->>Flask: POST /api/generate-chart
        Flask->>API: LLM builds ECharts config
    end
```

---

## 3. Pre-graph bootstrap (before LangGraph START)

Three independent DB/API calls run **in parallel** inside `JeenInsightsAgent.process_question()` before the graph starts. Results seed `AgentState`.

```mermaid
flowchart LR
    subgraph Parallel["asyncio.gather()"]
        A["SimpleUserResolver\nresolve_user(user_context)"]
        B["history.get_conversation_context\n(session_id, limit=2)"]
        C["history.log_query\nuser_id + session_id + question\n→ query_id"]
        D["_load_catalog(source_key)\nMCP or metadata DB bundle"]
    end

    A --> ST["AgentState.user_id"]
    B --> ST2["AgentState.conversation_history"]
    C --> ST3["AgentState.query_id"]
    D --> ST4["AgentState.metadata_bundle (seed)"]

    ST & ST2 & ST3 & ST4 --> INV["graph.ainvoke(initial_state)"]
```

| Step | Service | Storage / tool | Purpose |
|------|---------|----------------|---------|
| User resolution | `SimpleUserResolver` | Flask session → `user_context` | Per-user history, audit rows |
| Short-term memory | `ConversationHistoryService` | `insights_conversation_sessions` | Last 2 Q&As for follow-ups |
| Audit log | `ConversationHistoryService` | `insights_conversation_sessions` | `query_id` for trace & feedback |
| Catalog preload | `MetadataLoader` or `McpCatalogClient` | See §4 | Warm metadata bundle (optional; node reloads too) |

---

## 4. Catalog source: Metadata DB vs MCP

The `catalog_lookup` node (and pre-graph preload) route to **one** catalog provider based on `app_settings.catalog_source`.

```mermaid
flowchart TD
    SRC{"catalog_source\n(app_settings)"}
    SRC -->|db| DB["MetadataLoader.load_all(source_key)"]
    SRC -->|mcp| MCP["McpCatalogClient.load_all(source_key)"]

    DB --> TABLES[("PostgreSQL metadata DB\nmetadata_tables · metadata_columns\nmetadata_relationships · metadata_business_terms\nknowledge_pairs · metadata_sources")]

    MCP --> CACHE{"insights_mcp_cache\nL2 cache hit?"}
    CACHE -->|miss| TOOLS["MCP JSON-RPC tools"]
    TOOLS --> LC["list_connections"]
    TOOLS --> GCP["get_catalog_prompt(connection_id)"]
    GCP --> PARSE["_parse_catalog_markdown()\n→ same bundle keys as DB path"]
    PARSE --> BUNDLE["metadata_bundle dict\n(tables, columns, relationships,\nbusiness_terms, knowledge_pairs, sources)"]
    CACHE -->|hit| BUNDLE
    DB --> BUNDLE

    BUNDLE --> PB["prompt_builder\njeen_insights_system.md + bundle"]
```

**MCP tools** (when catalog source = `mcp`):

| MCP tool | Catalog need | Returns |
|----------|--------------|---------|
| `list_connections` | `list_sources` | Connection list (`source_key` ↔ `connection_id`) |
| `get_catalog_prompt` | `list_tables` | Full markdown catalog (all sections) |
| `get_filtered_prompt` | `describe_table` | Filtered catalog (optional) |

---

## 5. LangGraph agent (core question → SQL → answer)

The compiled graph has **25 nodes** and 58 arcs. Every node appends a timed event to `state.trace` (shown in the UI developer panel). The full node reference, the arc table with routing conditions, and the conversation-memory design live in [agent-state-flow.md](./agent-state-flow.md) (diagrams: [agent-state-flow.drawio](./agent-state-flow.drawio)); the overview below is the same graph grouped by phase — the graph follows one route through it for a given question.

```mermaid
flowchart TD
    S([START]) -->|resume/confirmed analysis| CL
    S -->|default| CC

    subgraph Memory_Routing
        CC[context_composer] -->|ledger| FR[fused_router · LLM]
        FR -->|from_memory| MAG[memory_answer_generator · LLM]
        FR -->|history_lookup| HS[history_search · DB]
        FR -->|capability| CAP[capability_answer · LLM]
    end

    FR -->|greeting / out_of_scope / unsafe / clarify_route| RF
    FR -->|needs_query / needs_analysis| CL
    MAG -->|escape hatch: needs_query| CL
    MAG -->|computed table| TRC
    MAG -->|replay / answer| RF
    HS --> RF
    CAP --> RF

    subgraph Catalog_Filters
        CL[catalog_lookup · DB/MCP] -->|ok| FP[filter_planner · LLM]
        FP -->|no clarification| FG[filter_grounder · DB]
        FG -->|prior_refs| PDB[prior_data_binder · LLM]
    end
    CL -->|catalog_blocked| RF
    CL -->|resume & on branch| AG
    FP -->|clarification| RF
    FG -->|clarification| RF
    FG -->|needs_analysis| AP
    FG -->|needs_query| PB
    PDB -->|bound values| PB
    PDB -->|too many values| RF

    subgraph ML_branch
        AP[analysis_planner · LLM] -->|params| AG[analysis_guard · DB]
        AG -->|guards pass & confirmed| ASQ[analysis_sql]
        AR[analysis_run · ML]
    end
    AP -->|clarify| RF
    AP -->|fallback → SQL| PB
    AG -->|guard fail / confirm card / error| RF
    ASQ -->|error| RF
    ASQ -->|sql| SV

    subgraph SQL_Exec
        PB[prompt_builder] --> SG[sql_generator · LLM]
        SG -->|sql| SV[sqlglot_validate · sqlglot]
        SV -->|valid| DC[dlp_check]
        DC -->|safe| EQ[execute_query · SqlRunner]
        EQ -->|rows, SQL path| EFC[empty_filter_result_check]
        EFC -->|no reground| TRC[trivial_result_check]
        TRC -->|non-trivial & eval on| FEA[fused_eval_analytics · LLM]
        FC[feedback_classifier]
    end
    SG -->|clarification| RF
    SV -->|error, SQL path| FC
    SV -->|error, ML path| RF
    DC -->|blocked| RF
    EQ -->|error, SQL path| FC
    EQ -->|error, ML path| RF
    EQ -->|rows, ML path| AR
    AR -->|ok| TRC
    AR -->|guard fail / error| RF
    EFC -->|reground| FC
    TRC -->|trivial or eval off| RF
    FEA -->|answers intent or ML / memory path| RF
    FEA -->|wrong| FC
    FC -->|syntax / exec / semantic| SG
    FC -->|missing_table| CL
    FC -->|resolve_filters| FG
    FC -->|exhausted| RF

    subgraph Tail
        RF[response_formatter] --> STM[save_to_memory · DB] --> OL[observability_log]
    end
    OL --> E([END])
```

Only the pre-graph bootstrap in §3 runs concurrent work with `asyncio.gather()`. The LangGraph trace itself is sequential per route, even though the UI and docs lay out alternative branches side-by-side.

### Router outcomes (`fused_router`)

| Route | Next path | Meaning |
|-------|-----------|---------|
| `needs_query` | `catalog_lookup` → filters → SQL pipeline (via `prior_data_binder` when `prior_refs` is set) | Normal analytics question, possibly building on a prior result |
| `needs_analysis` | `catalog_lookup` → filters → ML branch | Prediction / anomaly / driver question (see [ml-skills.md](./ml-skills.md)) |
| `from_memory` | `memory_answer_generator` | About a prior turn's answer or data: replay, compute over the stored rows, or answer from the ledger |
| `history_lookup` | `history_search` | "Did I ask about X last week?" — searched in the History log |
| `capability` | `capability_answer` | A question about the assistant itself |
| `clarify_route` | `response_formatter` | SQL-vs-ML genuinely ambiguous; the user picks |
| `greeting` | `response_formatter` | Short-circuit hello (regex, no LLM) |
| `out_of_scope` | `response_formatter` | Not a data question |
| `unsafe` | `response_formatter` | Blocked intent |

### Tools used inside the graph

| Node | Tool / library | Role |
|------|----------------|------|
| `sqlglot_validate` | **sqlglot** | Parse SQL, single read-only statement, allowlists, verified filters preserved |
| `dlp_check` | **DLP regex rules** | Block sensitive columns / dangerous patterns |
| `execute_query` | **SqlRunner** (Postgres / Trino / Databricks) | Read-only `SELECT`/`WITH` on the user's data source |
| `catalog_lookup` | **MetadataLoader** or **McpCatalogClient** | Schema + business context for prompts |
| `filter_grounder` | **value probes** (metadata / MCP / bounded SQL) | Verify literal values before SQL is written |
| `memory_answer_generator`, `prior_data_binder` | **PriorResultStore** + **SnapshotSqlEngine** | Recover a prior result's rows (cache → snapshot → re-run) and compute over them in the metadata Postgres, exposed as `insights_mem_*` CTEs over `jsonb_to_recordset` bind parameters (no tables created) |
| `history_search` | **ConversationHistoryService.search_turns** | Keyword + time-window search of the History log |
| `save_to_memory` | **ConversationHistoryService** | Update row: SQL, latency, tokens, status, preview, artifact, snapshot |

### LLM prompts (file → node)

| Prompt file | Used by |
|-------------|---------|
| `fused_router.md` | `fused_router` |
| `capability_answer.md` | `capability_answer` |
| `memory_answer.md` | `memory_answer_generator` |
| `sql_filter_planner.md` | `filter_planner` |
| `prior_data_binder.md` | `prior_data_binder` |
| `jeen_insights_system.md` | `prompt_builder` (injected catalog) |
| `sql_generator.md` | `sql_generator` (retry message) |
| `analysis_planner.md` | `analysis_planner` |
| `fused_eval_analytics.md` / `analysis_narration.md` | `fused_eval_analytics` (SQL results / ML results) |

Prompts are loaded via `PromptLoader` / `PromptCache` (DB-backed with file fallback).

---

## 6. SQL execution tool

```mermaid
flowchart LR
    SQL["generated_sql"] --> RO{"is_read_only_sql?\nSELECT / WITH only"}
    RO -->|no| ERR["exec_error → feedback_classifier"]
    RO -->|yes| POOL["asyncpg pool\n(user connection string)"]
    POOL --> TX["READ ONLY transaction"]
    TX --> ROWS["query_result\n{columns, rows}"]
```

Implementation: `src/tools/sql_tool.py` → `PostgresSqlRunner.run_sql()`

---

## 7. Response back to the UI

```mermaid
flowchart LR
    RF["response_formatter"] --> FMT["formatted_response"]
    FMT --> API["POST /api/query response"]
    API --> UI["displayResults()"]

    subgraph Payload["Key fields in JSON"]
        P1["question · sql · results"]
        P2["query_id · session_id"]
        P3["structured_prompt · metrics"]
        P4["trace[] per-node timings"]
        P5["answer · error · clarification"]
    end

    FMT --> Payload
    UI --> TBL["Results table"]
    UI --> DEV["Developer panel\nPrompt · SQL · Trace"]
    UI --> HIST["Sidebar history\n(recent / pinned)"]
```

After the graph completes, `JeenInsightsAgent` attaches the full **execution trace** (including `save_to_memory` and `observability_log`) to the API response for the developer log panel.

---

## 8. Optional follow-up flows (same session)

These run **after** the main query returns rows. They use separate API endpoints and (for insights) a small LangGraph subgraph.
The Developer panel shows them as **Post-query work** so their latency is visible without mixing it into the main `/api/ask` LangGraph trace.

```mermaid
flowchart TB
    subgraph MainDone["Main query complete"]
        RES["results + sql + query_id"]
    end

    subgraph Insights["AI Insights (background)"]
        IM["InsightsManager"]
        GE["POST /api/generate-insights/stream"]
        EVAL["insights_eval_graph\n(single eval node)"]
        DBI["insights_query_insights table"]
    end

    subgraph Charts["Chart view"]
        CM["ChartManager"]
        GC["POST /api/generate-chart"]
        EC["POST /api/edit-chart · chart chat"]
        ECH["Apache ECharts render"]
    end

    subgraph DevPanel["Developer panel"]
        PQ["Post-query work cards\nTTFT · LLM · tokens · render status"]
    end

    subgraph Autocomplete["Ask box helpers (separate)"]
        AC1["@ tables · # columns · / templates"]
        AC2["GET /api/knowledge-* · suggest-questions"]
    end

    RES --> IM --> GE --> EVAL --> DBI
    RES --> CM --> GC --> ECH
    CM --> EC --> ECH
    GE --> PQ
    GC --> PQ
```

| Feature | Endpoint | LLM / graph |
|---------|----------|-------------|
| Insights summary | `/api/generate-insights/stream` | `build_insights_eval_graph` → `fused_eval_analytics`; streamed TTFT, LLM time, and tokens appear in Developer panel post-query work |
| Chart generation | `/api/generate-chart` | LLM chart-spec decision + server-built ECharts option; request/render/cache status appears in Developer panel post-query work |
| Chart refinement | `/api/edit-chart` | `chart_editor.md` + client `chartOperators` |
| Autocomplete `/` | `/api/knowledge-questions` | No LLM (DB `knowledge_pairs`) |
| Autocomplete tier 3 | `/api/suggest-questions` | `autocomplete_suggestions.md` |

---

## 9. Persistence & user scoping

```mermaid
flowchart LR
    subgraph PerQuery["Per query row"]
        ICS["insights_conversation_sessions\nuser_id · session_id · source_key\nquestion · sql · status · tokens · trace metadata"]
    end

    subgraph PerUser["Per user + connection"]
        PIN["insights_pinned_questions"]
        REC["Recent questions\n(distinct from ICS)"]
        LOG["History log drawer\nfull audit list"]
    end

    subgraph Session["Browser session"]
        SID["currentSessionId UUID\n(follow-up memory)"]
        COOKIE["Flask session cookie\n(user_id for API)"]
    end

    ICS --> REC & LOG
    COOKIE --> ICS
    SID --> ICS
```

---

## 10. Source files (quick index)

| Layer | Path |
|-------|------|
| Browser | `src/static/script.js`, `src/templates/index.html` |
| Flask proxy | `src/ui_app.py` |
| Query API | `src/api/routes/query.py` |
| Agent orchestrator | `src/agent/jeen_insights_agent.py` |
| LangGraph graph | `src/agent/langgraph_agent/graph.py` |
| Graph nodes | `src/agent/langgraph_agent/nodes/*.py` |
| SQL tool | `src/tools/sql_tool.py` |
| Metadata DB loader | `src/metadata/metadata_loader.py` |
| MCP catalog client | `src/metadata/mcp_catalog_client.py` |
| Conversation history | `src/agent/conversation_history.py` |
| Insights subgraph | `src/api/routes/insights.py` |
| Charts | `src/api/routes/charts.py`, `src/static/chart-feature/` |
| LLM service | `src/agent/llm_service.py` (`LangChainLlmService`) |

---

## Related docs

- [agent-state-flow.md](./agent-state-flow.md) — LangGraph-only diagram and node reference
- [../README.md](../README.md) — API endpoint list and architecture overview
- [../PROMPTS.md](../PROMPTS.md) — Prompt inventory
