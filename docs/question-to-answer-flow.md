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
        LG["16 nodes: router → SQL → validate → execute → eval → format"]
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

The compiled graph has **16 nodes**. Every node appends a timed event to `state.trace` (shown in the UI developer panel).

```mermaid
flowchart TD
    S([▶ START])
    E([⏹ END])

    MSC["🧠 memory_shrink_check"]
    MS["🤏 memory_summarizer · LLM"]
    FR["🔀 fused_router · LLM"]
    MAG["💬 memory_answer_generator · LLM"]
    CL["📦 catalog_lookup · DB/MCP"]
    PB["🔧 prompt_builder"]
    SG["🧠 sql_generator · LLM"]
    SV["✅ sqlglot_validate · sqlglot"]
    DC["🛡 dlp_check · regex patterns"]
    EQ["▶ execute_query · PostgresSqlRunner"]
    TRC["⚡ trivial_result_check"]
    FEA["📊 fused_eval_analytics · LLM"]
    FC["🔁 feedback_classifier"]
    RF["📋 response_formatter"]
    STM["💾 save_to_memory · DB"]
    OL["🪵 observability_log"]

    S --> MSC
    MSC -->|over token budget| MS --> FR
    MSC -->|within budget| FR

    FR -->|needs_query| CL
    FR -->|from_memory| MAG
    FR -->|out_of_scope / unsafe / greeting| RF

    MAG -->|answer from history| RF
    MAG -->|needs fresh data| CL

    CL --> PB --> SG

    SG -->|SQL| SV
    SG -->|clarification| RF

    SV -->|valid| DC
    SV -->|syntax / unknown table| FC

    DC -->|safe| EQ
    DC -->|blocked column/pattern| RF

    EQ -->|rows| TRC
    EQ -->|exec error| FC

    TRC -->|trivial or eval off| RF
    TRC -->|needs eval| FEA

    FEA -->|answers intent| RF
    FEA -->|wrong result| FC

    FC -->|retries left| SG
    FC -->|missing table| CL
    FC -->|exhausted| RF

    RF --> STM --> OL --> E

    classDef llm fill:#ede9fe,stroke:#7c3aed,color:#4c1d95,font-weight:bold
    classDef db fill:#d1fae5,stroke:#059669,color:#064e3b,font-weight:bold
    classDef logic fill:#f1f5f9,stroke:#64748b,color:#1e293b
    classDef tool fill:#ffedd5,stroke:#ea580c,color:#7c2d12,font-weight:bold
    classDef term fill:#0f172a,stroke:#0f172a,color:#f8fafc,font-weight:bold

    class MS,FR,MAG,SG,FEA llm
    class CL,EQ,STM db
    class MSC,PB,TRC,FC,RF,OL logic
    class SV,DC tool
    class S,E term
```

### Router outcomes (`fused_router`)

| Route | Next path | Meaning |
|-------|-----------|---------|
| `needs_query` | `catalog_lookup` → SQL pipeline | Normal analytics question |
| `from_memory` | `memory_answer_generator` | Answerable from session history |
| `greeting` | `response_formatter` | Short-circuit hello |
| `out_of_scope` | `response_formatter` | Not a data question |
| `unsafe` | `response_formatter` | Blocked intent |

### Tools used inside the graph

| Node | Tool / library | Role |
|------|----------------|------|
| `sqlglot_validate` | **sqlglot** | Parse SQL, verify tables exist in `known_tables` |
| `dlp_check` | **DLP regex rules** | Block sensitive columns / dangerous patterns |
| `execute_query` | **PostgresSqlRunner** | Read-only `SELECT`/`WITH` on user data source |
| `catalog_lookup` | **MetadataLoader** or **McpCatalogClient** | Schema + business context for prompts |
| `save_to_memory` | **ConversationHistoryService** | Update row: SQL, latency, tokens, status, preview |

### LLM prompts (file → node)

| Prompt file | Used by |
|-------------|---------|
| `memory_summarizer.md` | `memory_summarizer` |
| `fused_router.md` | `fused_router` |
| `memory_answer.md` | `memory_answer_generator` |
| `jeen_insights_system.md` | `prompt_builder` (injected catalog) |
| `sql_generator.md` | `sql_generator` |
| `fused_eval_analytics.md` | `fused_eval_analytics` |

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

```mermaid
flowchart TB
    subgraph MainDone["Main query complete"]
        RES["results + sql + query_id"]
    end

    subgraph Insights["AI Insights (background)"]
        IM["InsightsManager"]
        GE["POST /api/generate-insights"]
        EVAL["insights_eval_graph\n(single eval node)"]
        DBI["insights_query_insights table"]
    end

    subgraph Charts["Chart view"]
        CM["ChartManager"]
        GC["POST /api/generate-chart"]
        EC["POST /api/edit-chart · chart chat"]
        ECH["Apache ECharts render"]
    end

    subgraph Autocomplete["Ask box helpers (separate)"]
        AC1["@ tables · # columns · / templates"]
        AC2["GET /api/knowledge-* · suggest-questions"]
    end

    RES --> IM --> GE --> EVAL --> DBI
    RES --> CM --> GC --> ECH
    CM --> EC --> ECH
```

| Feature | Endpoint | LLM / graph |
|---------|----------|-------------|
| Insights summary | `/api/generate-insights` | `build_insights_eval_graph` → `fused_eval_analytics` |
| Chart generation | `/api/generate-chart` | Direct LLM → ECharts JSON |
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
