#!/usr/bin/env python3
"""Generate docs/question-to-answer-flow.drawio from documented flows."""

from __future__ import annotations

import html
import uuid
from pathlib import Path
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

OUT = Path(__file__).resolve().parents[1] / "docs" / "question-to-answer-flow.drawio"

# ── styles ────────────────────────────────────────────────────────────────────
SWIM = "swimlane;whiteSpace=wrap;html=1;startSize=28;fillColor=#dae8fc;strokeColor=#6c8ebf;fontStyle=1;fontSize=11;"
UI = "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;fontSize=11;"
API = "rounded=1;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;fontSize=11;"
GRAPH = "rounded=1;whiteSpace=wrap;html=1;fillColor=#e1d5e7;strokeColor=#9673a6;fontSize=11;"
DB = "rounded=1;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;fontSize=11;"
LOGIC = "rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontSize=11;"
LLM = "rounded=1;whiteSpace=wrap;html=1;fillColor=#e1d5e7;strokeColor=#9673a6;fontStyle=1;fontSize=11;"
TOOL = "rounded=1;whiteSpace=wrap;html=1;fillColor=#ffe6cc;strokeColor=#d79b00;fontSize=11;"
TERM = "ellipse;whiteSpace=wrap;html=1;fillColor=#1e293b;strokeColor=#0f172a;fontColor=#ffffff;fontSize=11;fontStyle=1;"
DIAMOND = "rhombus;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;fontSize=10;"
EDGE = "edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;fontSize=10;"
ACTOR = "shape=umlActor;verticalLabelPosition=bottom;verticalAlign=top;html=1;outlineConnect=0;fillColor=#dae8fc;strokeColor=#6c8ebf;fontSize=10;"
LIFELINE = "shape=umlLifeline;perimeter=lifelinePerimeter;whiteSpace=wrap;html=1;container=1;collapsible=0;recursiveResize=0;outlineConnect=0;fillColor=#f5f5f5;strokeColor=#666666;fontSize=10;"


class DiagramBuilder:
    def __init__(self) -> None:
        self._n = 2
        self.cells: list[dict] = []

    def nid(self) -> str:
        self._n += 1
        return str(self._n)

    def box(self, label: str, x: float, y: float, w: float, h: float, style: str) -> str:
        i = self.nid()
        self.cells.append({"id": i, "vertex": True, "value": label, "x": x, "y": y, "w": w, "h": h, "style": style})
        return i

    def swim(self, label: str, x: float, y: float, w: float, h: float) -> str:
        return self.box(label, x, y, w, h, SWIM)

    def edge(self, src: str, tgt: str, label: str = "") -> None:
        i = self.nid()
        self.cells.append({"id": i, "vertex": False, "source": src, "target": tgt, "value": label, "style": EDGE})

    def to_mx(self, pw: int, ph: int) -> Element:
        model = Element(
            "mxGraphModel",
            {
                "dx": "1422",
                "dy": "794",
                "grid": "1",
                "gridSize": "10",
                "guides": "1",
                "tooltips": "1",
                "connect": "1",
                "arrows": "1",
                "fold": "1",
                "page": "1",
                "pageScale": "1",
                "pageWidth": str(pw),
                "pageHeight": str(ph),
                "math": "0",
                "shadow": "0",
            },
        )
        root = SubElement(model, "root")
        SubElement(root, "mxCell", {"id": "0"})
        SubElement(root, "mxCell", {"id": "1", "parent": "0"})
        for c in self.cells:
            if c.get("vertex"):
                cell = SubElement(
                    root,
                    "mxCell",
                    {
                        "id": c["id"],
                        "value": c["value"],
                        "style": c["style"],
                        "vertex": "1",
                        "parent": "1",
                    },
                )
                SubElement(
                    cell,
                    "mxGeometry",
                    {"x": str(c["x"]), "y": str(c["y"]), "width": str(c["w"]), "height": str(c["h"]), "as": "geometry"},
                )
            else:
                cell = SubElement(
                    root,
                    "mxCell",
                    {
                        "id": c["id"],
                        "value": c.get("value", ""),
                        "style": c["style"],
                        "edge": "1",
                        "parent": "1",
                        "source": c["source"],
                        "target": c["target"],
                    },
                )
                SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        return model


def page_overview() -> tuple[str, Element, int, int]:
    d = DiagramBuilder()
    d.swim("Browser", 40, 40, 220, 220)
    d.swim("Flask UI", 300, 40, 200, 140)
    d.swim("FastAPI", 540, 40, 240, 180)
    d.swim("Pre-graph (parallel)", 820, 40, 260, 200)
    d.swim("LangGraph", 1120, 40, 280, 120)
    d.swim("Data sources", 40, 300, 420, 150)

    qin = d.box("User types question", 60, 80, 180, 40, UI)
    ask = d.box("POST /api/ask", 60, 130, 180, 40, UI)
    disp = d.box("displayResults()", 60, 180, 180, 40, UI)
    ins = d.box("InsightsManager", 60, 230, 180, 35, UI)

    auth = d.box("Session auth\nuser_context", 320, 70, 160, 50, API)
    proxy = d.box("Proxy → /api/query", 320, 140, 160, 40, API)

    query = d.box("POST /api/query", 560, 60, 200, 40, API)
    agent = d.box("JeenInsightsAgent\n.process_question()", 560, 120, 200, 50, API)

    u = d.box("SimpleUserResolver", 840, 70, 220, 35, LOGIC)
    ctx = d.box("get_conversation_context", 840, 115, 220, 35, LOGIC)
    aud = d.box("log_query → query_id", 840, 160, 220, 35, LOGIC)
    cat = d.box("Catalog preload", 840, 205, 220, 35, LOGIC)

    lg = d.box("16 nodes:\nrouter → SQL → execute → eval", 1140, 70, 240, 60, GRAPH)

    meta = d.box("Metadata DB", 60, 340, 170, 45, DB)
    mcp = d.box("MCP server", 250, 340, 170, 45, DB)
    pg = d.box("User PostgreSQL", 60, 395, 170, 45, DB)
    hist = d.box("insights_conversation_sessions", 250, 395, 200, 45, DB)

    chart = d.box("ChartManager", 540, 300, 180, 40, UI)
    ge = d.box("POST /api/generate-chart", 740, 300, 200, 40, API)

    for a, b in [(qin, ask), (ask, auth), (auth, proxy), (proxy, query), (query, agent),
                 (agent, u), (agent, lg), (lg, meta), (lg, disp), (disp, ins), (disp, chart), (chart, ge)]:
        d.edge(a, b)
    return "1 - End-to-end overview", d.to_mx(1500, 520), 1500, 520


def page_sequence() -> tuple[str, Element, int, int]:
    d = DiagramBuilder()
    actors = [
        ("User", 40, ACTOR),
        ("Browser\nscript.js", 160, LIFELINE),
        ("Flask UI\n:8501", 320, LIFELINE),
        ("FastAPI\n:8000", 480, LIFELINE),
        ("JeenInsightsAgent", 640, LIFELINE),
        ("LangGraph", 820, LIFELINE),
    ]
    ids = []
    for name, x, style in actors:
        ids.append(d.box(name, x, 40, 100, 60 if "Actor" in style else 500, style))

    steps = [
        (0, 1, "Submit question"),
        (1, 1, "requireConnection()"),
        (1, 2, "POST /api/ask"),
        (2, 3, "POST /api/query\n+ user_context"),
        (3, 4, "process_question()"),
        (4, 5, "ainvoke(state)"),
        (5, 4, "final_state + trace"),
        (4, 3, "formatted_response"),
        (3, 2, "QueryResponse JSON"),
        (2, 1, "results + trace"),
        (1, 1, "displayResults()"),
    ]
    y = 120
    for src, tgt, lbl in steps:
        if src == tgt:
            continue
        d.edge(ids[src], ids[tgt], lbl)
        y += 30

    note = d.box(
        "Flask injects user_context from session cookie\nOptional: /api/generate-insights, /api/generate-chart",
        40, 560, 880, 50,
        "text;html=1;strokeColor=none;fillColor=#fff2cc;align=left;verticalAlign=middle;fontSize=11;",
    )
    return "2 - UI to API sequence", d.to_mx(1000, 680), 1000, 680


def page_pregraph() -> tuple[str, Element, int, int]:
    d = DiagramBuilder()
    d.swim("asyncio.gather() — parallel", 40, 40, 920, 160)
    a = d.box("SimpleUserResolver\nresolve_user(user_context)", 60, 90, 200, 50, LOGIC)
    b = d.box("get_conversation_context\n(session_id, limit=2)", 280, 90, 200, 50, DB)
    c = d.box("log_query\n→ query_id", 500, 90, 200, 50, DB)
    e = d.box("_load_catalog(source_key)\nMCP or metadata DB", 720, 90, 220, 50, DB)

    inv = d.box("graph.ainvoke(initial_state)", 360, 260, 280, 50, GRAPH)
    sa = d.box("AgentState.user_id", 60, 220, 180, 35, API)
    sb = d.box("AgentState.conversation_history", 260, 220, 200, 35, API)
    sc = d.box("AgentState.query_id", 480, 220, 180, 35, API)
    sd = d.box("AgentState.metadata_bundle", 680, 220, 200, 35, API)

    for x, y in [(a, sa), (b, sb), (c, sc), (e, sd)]:
        d.edge(x, y)
    for z in [sa, sb, sc, sd]:
        d.edge(z, inv)
    return "3 - Pre-graph bootstrap", d.to_mx(1000, 380), 1000, 380


def page_catalog() -> tuple[str, Element, int, int]:
    d = DiagramBuilder()
    src = d.box("catalog_source\n(app_settings)", 360, 40, 160, 70, DIAMOND)
    db = d.box("MetadataLoader.load_all", 120, 160, 200, 50, DB)
    mcp = d.box("McpCatalogClient.load_all", 560, 160, 220, 50, DB)
    tables = d.box("PostgreSQL metadata DB\nmetadata_* · knowledge_pairs", 80, 260, 280, 60, DB)
    cache = d.box("insights_mcp_cache\nL2 hit?", 540, 260, 160, 60, DIAMOND)
    tools = d.box("MCP JSON-RPC", 760, 260, 140, 50, API)
    lc = d.box("list_connections", 720, 340, 140, 40, API)
    gcp = d.box("get_catalog_prompt", 880, 340, 160, 40, API)
    parse = d.box("_parse_catalog_markdown()", 760, 420, 200, 40, LOGIC)
    bundle = d.box("metadata_bundle dict", 360, 480, 280, 60, API)
    pb = d.box("prompt_builder\njeen_insights_system.md", 360, 580, 280, 50, LOGIC)

    d.edge(src, db, "db")
    d.edge(src, mcp, "mcp")
    d.edge(db, tables)
    d.edge(db, bundle)
    d.edge(mcp, cache)
    d.edge(cache, bundle, "hit")
    d.edge(cache, tools, "miss")
    d.edge(tools, lc)
    d.edge(tools, gcp)
    d.edge(gcp, parse)
    d.edge(parse, bundle)
    d.edge(bundle, pb)
    return "4 - Catalog DB vs MCP", d.to_mx(1100, 700), 1100, 700


def page_langgraph() -> tuple[str, Element, int, int]:
    d = DiagramBuilder()
    s = d.box("START", 400, 20, 80, 40, TERM)
    e = d.box("END", 400, 1180, 80, 40, TERM)

    msc = d.box("memory_shrink_check", 340, 80, 200, 40, LOGIC)
    ms = d.box("memory_summarizer (LLM)", 120, 160, 200, 40, LLM)
    fr = d.box("fused_router (LLM)", 340, 240, 200, 40, LLM)
    mag = d.box("memory_answer_generator (LLM)", 80, 320, 240, 40, LLM)
    cl = d.box("catalog_lookup (DB/MCP)", 340, 400, 200, 40, DB)
    pb = d.box("prompt_builder", 340, 480, 200, 40, LOGIC)
    sg = d.box("sql_generator (LLM)", 340, 560, 200, 40, LLM)
    sv = d.box("sqlglot_validate", 340, 640, 200, 40, TOOL)
    dc = d.box("dlp_check", 340, 720, 200, 40, TOOL)
    eq = d.box("execute_query (PostgresSqlRunner)", 340, 800, 240, 40, DB)
    trc = d.box("trivial_result_check", 340, 880, 200, 40, LOGIC)
    fea = d.box("fused_eval_analytics (LLM)", 120, 960, 220, 40, LLM)
    fc = d.box("feedback_classifier", 680, 640, 200, 40, LOGIC)
    rf = d.box("response_formatter", 340, 1040, 200, 40, LOGIC)
    stm = d.box("save_to_memory (DB)", 340, 1100, 200, 40, DB)
    ol = d.box("observability_log", 340, 1140, 200, 40, LOGIC)

    d.edge(s, msc)
    d.edge(msc, ms, "over budget")
    d.edge(msc, fr, "within budget")
    d.edge(ms, fr)
    d.edge(fr, cl, "needs_query")
    d.edge(fr, mag, "from_memory")
    d.edge(fr, rf, "out_of_scope / unsafe / greeting")
    d.edge(mag, rf, "answer ready")
    d.edge(mag, cl, "needs fresh data")
    d.edge(cl, pb)
    d.edge(pb, sg)
    d.edge(sg, sv, "SQL")
    d.edge(sg, rf, "clarification")
    d.edge(sv, dc, "valid")
    d.edge(sv, fc, "syntax error")
    d.edge(dc, eq, "safe")
    d.edge(dc, rf, "blocked")
    d.edge(eq, trc, "rows")
    d.edge(eq, fc, "exec error")
    d.edge(trc, rf, "trivial / eval off")
    d.edge(trc, fea, "needs eval")
    d.edge(fea, rf, "answers intent")
    d.edge(fea, fc, "wrong result")
    d.edge(fc, sg, "retry SQL")
    d.edge(fc, cl, "missing table")
    d.edge(fc, rf, "exhausted")
    d.edge(rf, stm)
    d.edge(stm, ol)
    d.edge(ol, e)

    leg = d.box(
        "Purple=LLM · Green=DB · Gray=Logic · Orange=Tools (sqlglot, DLP)",
        40, 20, 260, 50,
        "text;html=1;strokeColor=none;fillColor=#f5f5f5;align=left;fontSize=10;",
    )
    return "5 - LangGraph agent (16 nodes)", d.to_mx(960, 1280), 960, 1280


def page_sql() -> tuple[str, Element, int, int]:
    d = DiagramBuilder()
    sql = d.box("generated_sql", 40, 80, 140, 40, API)
    ro = d.box("is_read_only_sql?\nSELECT / WITH only", 220, 70, 160, 60, DIAMOND)
    err = d.box("exec_error →\nfeedback_classifier", 220, 200, 180, 50, LOGIC)
    pool = d.box("asyncpg pool", 420, 80, 140, 40, DB)
    tx = d.box("READ ONLY transaction", 600, 80, 160, 40, DB)
    rows = d.box("query_result\n{columns, rows}", 800, 80, 160, 50, API)
    d.edge(sql, ro)
    d.edge(ro, err, "no")
    d.edge(ro, pool, "yes")
    d.edge(pool, tx)
    d.edge(tx, rows)
    note = d.box("src/tools/sql_tool.py → PostgresSqlRunner.run_sql()", 40, 280, 400, 35,
                 "text;html=1;strokeColor=none;fillColor=#f5f5f5;align=left;fontSize=11;")
    return "6 - SQL execution tool", d.to_mx(1000, 360), 1000, 360


def page_response() -> tuple[str, Element, int, int]:
    d = DiagramBuilder()
    rf = d.box("response_formatter", 40, 80, 160, 40, LOGIC)
    fmt = d.box("formatted_response", 240, 80, 160, 40, API)
    api = d.box("POST /api/query response", 440, 80, 180, 40, API)
    ui = d.box("displayResults()", 660, 80, 160, 40, UI)
    tbl = d.box("Results table", 660, 180, 140, 40, UI)
    dev = d.box("Developer panel\nPrompt · SQL · Trace", 820, 180, 180, 50, UI)
    hist = d.box("Sidebar history", 1020, 180, 140, 40, UI)
    pay = d.box("JSON: question, sql, results,\nquery_id, session_id, trace, metrics", 240, 180, 320, 60, API)
    for a, b in [(rf, fmt), (fmt, api), (api, ui), (fmt, pay), (ui, tbl), (ui, dev), (ui, hist)]:
        d.edge(a, b)
    return "7 - Response to UI", d.to_mx(1200, 300), 1200, 300


def page_followups() -> tuple[str, Element, int, int]:
    d = DiagramBuilder()
    res = d.box("Main query complete\nresults + sql + query_id", 360, 40, 280, 50, API)
    d.swim("AI Insights", 40, 120, 320, 200)
    d.swim("Charts", 400, 120, 320, 200)
    d.swim("Autocomplete", 760, 120, 300, 200)
    im = d.box("InsightsManager", 60, 170, 160, 40, UI)
    ge = d.box("POST /api/generate-insights", 60, 230, 200, 40, API)
    ev = d.box("insights_eval_graph", 60, 290, 180, 40, GRAPH)
    dbi = d.box("insights_query_insights", 60, 350, 200, 40, DB)
    cm = d.box("ChartManager", 420, 170, 160, 40, UI)
    gc = d.box("POST /api/generate-chart", 420, 230, 200, 40, API)
    ec = d.box("POST /api/edit-chart", 420, 290, 180, 40, API)
    ech = d.box("Apache ECharts", 420, 350, 160, 40, UI)
    ac1 = d.box("@ tables · # columns · / templates", 780, 170, 260, 40, UI)
    ac2 = d.box("GET knowledge-* · suggest-questions", 780, 230, 260, 40, API)
    for chain in [(res, im), (im, ge), (ge, ev), (ev, dbi), (res, cm), (cm, gc), (gc, ech), (cm, ec), (ec, ech)]:
        d.edge(*chain)
    return "8 - Optional follow-ups", d.to_mx(1100, 400), 1100, 400


def page_persistence() -> tuple[str, Element, int, int]:
    d = DiagramBuilder()
    d.swim("Browser session", 40, 40, 280, 160)
    d.swim("Per query row", 360, 40, 360, 160)
    d.swim("Per user + connection", 760, 40, 360, 160)
    sid = d.box("currentSessionId (UUID)", 60, 90, 200, 40, UI)
    cookie = d.box("Flask session cookie\nuser_id", 60, 150, 200, 40, UI)
    ics = d.box("insights_conversation_sessions", 380, 90, 300, 80, DB)
    pin = d.box("insights_pinned_questions", 780, 90, 200, 40, DB)
    rec = d.box("Recent questions", 780, 150, 200, 40, DB)
    log = d.box("History log drawer", 1000, 90, 180, 40, DB)
    d.edge(cookie, ics)
    d.edge(sid, ics)
    d.edge(ics, rec)
    d.edge(ics, log)
    return "9 - Persistence and user scoping", d.to_mx(1180, 260), 1180, 260


def build_mxfile() -> str:
    pages = [
        page_overview(),
        page_sequence(),
        page_pregraph(),
        page_catalog(),
        page_langgraph(),
        page_sql(),
        page_response(),
        page_followups(),
        page_persistence(),
    ]
    mxfile = Element(
        "mxfile",
        {
            "host": "app.diagrams.net",
            "agent": "jeen-insight-generator",
            "version": "22.1.0",
            "type": "device",
        },
    )
    for name, model, pw, ph in pages:
        diag_id = str(uuid.uuid4())
        diagram = SubElement(
            mxfile,
            "diagram",
            {"id": diag_id, "name": name},
        )
        # draw.io expects diagram content as escaped XML string in some versions;
        # uncompressed child mxGraphModel works in diagrams.net desktop & web.
        diagram.append(model)

    rough = tostring(mxfile, encoding="unicode")
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent="  ")


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build_mxfile(), encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
