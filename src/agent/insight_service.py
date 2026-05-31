"""Insight generation service for analyzing query results."""

from typing import Any, Dict, List, Optional
import json
import pandas as pd
from pathlib import Path


async def generate_insights(
    dataset: Any,
    context: Dict[str, Any],
    original_question: str,
    llm_service: Any = None,
    prompt_template: Optional[str] = None,
    model_override: Any = None,
) -> Dict[str, Any]:
    """
    Analyzes a dataset and returns insights.
    
    Args:
        dataset: DataFrame or dict - the query results
        context: dict - contains 'documentation' (business rules)
        original_question: str - the user's original question
        llm_service: LLM service instance for calling the model
    
    Returns:
        dict: {
            "summary": str (one-line summary),
            "findings": [list of insight strings],
            "suggestions": [list of suggestion strings],
            "prompt": str (the prompt used),
            "system_message": str (the system message used)
        }
    """
    system_message = "You are a senior data analyst specialized in finding actionable insights."
    
    try:
        # Convert dataset to DataFrame if needed
        if isinstance(dataset, dict):
            if 'rows' in dataset and 'columns' in dataset:
                df = pd.DataFrame(dataset['rows'], columns=dataset['columns'])
            else:
                df = pd.DataFrame(dataset)
        elif isinstance(dataset, pd.DataFrame):
            df = dataset
        else:
            prompt = "N/A - Unsupported dataset format"
            return _empty_insights("Unsupported dataset format", prompt, system_message)
        
        # Check if dataset is empty or too small
        if df.empty:
            prompt = "N/A - No data returned from query"
            return _empty_insights("No data returned from query", prompt, system_message)
        
        if len(df) == 1:
            prompt = "N/A - Single record, no patterns to analyze"
            return _empty_insights("Single record returned, no patterns to analyze", prompt, system_message)
        
        # Prepare dataset summary for LLM
        dataset_summary = _prepare_dataset_summary(df)
        
        # Build prompt
        prompt = _build_insight_prompt(
            dataset_summary=dataset_summary,
            context=context,
            original_question=original_question,
            prompt_template=prompt_template,
        )
        
        # Call LLM if service is provided
        if llm_service is None:
            return _empty_insights("LLM service not available", prompt, system_message)
        
        # Generate insights using LLM (await the async call)
        response = await llm_service.generate(
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=1024,
            model_override=model_override,
        )
        
        # Parse LLM response
        content = response.get("content", "")
        insights = _parse_insights_response(content)
        
        # Include the prompt used
        insights["prompt"] = prompt
        insights["system_message"] = system_message
        
        return insights
        
    except Exception as e:
        return {
            "summary": "Unable to generate insights",
            "findings": [f"Error: {str(e)}"],
            "suggestions": [],
            "prompt": prompt if 'prompt' in locals() else "N/A - Error occurred before prompt generation",
            "system_message": system_message
        }


def _prepare_dataset_summary(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Prepare a summary of the dataset for the LLM.
    Does NOT send full dataset - only statistics and samples.
    """
    summary = {
        "row_count": len(df),
        "column_names": list(df.columns),
        "data_sample": "",
        "column_stats": ""
    }
    
    # Get first 10 rows as sample
    sample_df = df.head(10)
    summary["data_sample"] = sample_df.to_string(index=False)
    
    # Get column statistics
    stats_parts = []
    
    for col in df.columns:
        col_type = df[col].dtype
        
        if pd.api.types.is_numeric_dtype(col_type):
            # Numeric column statistics
            stats_parts.append(f"{col} (numeric):")
            stats_parts.append(f"  - Min: {df[col].min()}")
            stats_parts.append(f"  - Max: {df[col].max()}")
            stats_parts.append(f"  - Mean: {df[col].mean():.2f}")
            stats_parts.append(f"  - Median: {df[col].median()}")
        else:
            # Categorical column statistics
            unique_count = df[col].nunique()
            stats_parts.append(f"{col} (categorical):")
            stats_parts.append(f"  - Unique values: {unique_count}")
            
            # Top 3 values
            top_values = df[col].value_counts().head(3)
            if not top_values.empty:
                stats_parts.append(f"  - Top values: {', '.join([str(v) for v in top_values.index])}")
    
    summary["column_stats"] = "\n".join(stats_parts)
    
    return summary


def _build_insight_prompt(
    dataset_summary: Dict[str, Any],
    context: Dict[str, Any],
    original_question: str,
    prompt_template: Optional[str] = None,
) -> str:
    """Build the insight generation prompt.

    If *prompt_template* is provided (resolved from PromptCache) it is used.
    Otherwise the template file is read from disk as before.
    """
    if prompt_template is not None:
        template = prompt_template
    else:
        # Legacy fallback: read from disk directly.
        template_path = Path(__file__).parent.parent.parent / "templates" / "insight_prompt.txt"
        if template_path.exists():
            with open(template_path, "r", encoding="utf-8") as f:
                template = f.read()
        else:
            template = _FALLBACK_INSIGHT_TEMPLATE

    business_rules = ""
    if context and "documentation" in context:
        rules = context["documentation"]
        if isinstance(rules, list):
            business_rules = "\n".join([f"- {rule}" for rule in rules[:5]])
        else:
            business_rules = str(rules)
    if not business_rules:
        business_rules = "No specific business rules provided"

    return template.format(
        original_question=original_question,
        business_rules=business_rules,
        row_count=dataset_summary["row_count"],
        column_names=", ".join(dataset_summary["column_names"]),
        data_sample=dataset_summary["data_sample"],
        column_stats=dataset_summary["column_stats"],
    )


_FALLBACK_INSIGHT_TEMPLATE = """You are a senior data analyst. Analyze the query results and provide insights.

## RULES
- ONLY report findings you are highly confident about
- Every insight must be backed by specific numbers from the data
- Be concise — max 3-5 key findings
- Match the user's language (English/Hebrew)

## CONFIDENCE THRESHOLDS — REPORT ONLY IF:
- Differences: ≥ 20% from average/baseline
- Patterns: Clear majority (≥70%) or minority (≤20%)
- Trends: Consistent direction across data points

DO NOT report vague findings like "seems to" or "might be".

## USER'S QUESTION:
{original_question}

## BUSINESS RULES:
{business_rules}

## DATASET SUMMARY:
- Total rows: {row_count}
- Columns: {column_names}
- Data sample:
{data_sample}

## COLUMN STATISTICS:
{column_stats}

## OUTPUT FORMAT (respond in JSON):
{{
  "summary": "One sentence summarizing the key takeaway",
  "findings": [
    "Finding 1 with specific numbers",
    "Finding 2 with specific numbers"
  ],
  "suggestions": [
    "Follow-up question the user could ask next — phrased as a short question",
    "Another follow-up question (4-6 total)"
  ]
}}

If the dataset is too small or no meaningful insights exist, return:
{{
  "summary": "Insufficient data for meaningful insights",
  "findings": [],
  "suggestions": []
}}"""


def _parse_insights_response(content: str) -> Dict[str, Any]:
    """Parse LLM response into structured insights."""
    try:
        # Try to extract JSON from response
        content = content.strip()
        
        # Remove markdown code blocks if present
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        
        # Find JSON object
        if not content.startswith("{"):
            start_idx = content.find("{")
            end_idx = content.rfind("}")
            if start_idx != -1 and end_idx != -1:
                content = content[start_idx:end_idx+1]
        
        # Parse JSON
        insights = json.loads(content)
        
        # Validate structure
        if not isinstance(insights, dict):
            return _empty_insights("Invalid response format")

        # ── Normalise new prompt schema → internal shape ─────────────────────
        # New prompt produces: findings, anomalies, recommendations, suggestions
        # Internal shape:      findings (list),  suggestions (actions), followups (questions)

        # Merge anomalies into findings (they are just a specialised finding)
        anomalies = insights.pop("anomalies", None) or []
        if isinstance(anomalies, list) and anomalies:
            insights.setdefault("findings", [])
            insights["findings"] = list(insights["findings"]) + list(anomalies)

        # recommendations → keep as "suggestions" (actionable decisions)
        recommendations = insights.pop("recommendations", None)
        if recommendations and not insights.get("suggestions"):
            # Only use recommendations when there are no suggestions already
            # (old-format prompts put follow-up questions in suggestions)
            pass  # handled below

        # Old format: "suggestions" = follow-up questions
        # New format: "suggestions" = follow-up questions, "recommendations" = actions
        # Detect which format we have by checking if suggestions look like questions
        raw_suggestions = insights.get("suggestions") or []
        first_sug = raw_suggestions[0] if raw_suggestions else ""
        looks_like_questions = first_sug.strip().endswith("?")

        if looks_like_questions:
            # New format detected: suggestions = follow-up questions
            insights["followups"] = raw_suggestions
            insights["suggestions"] = [str(r) for r in (recommendations or []) if r]
        else:
            # Old format: suggestions = questions (already correct for followups path)
            insights.setdefault("followups", raw_suggestions)

        # Ensure required fields
        insights.setdefault("summary", "Analysis complete")
        insights.setdefault("findings", [])
        insights.setdefault("suggestions", [])
        insights.setdefault("followups", [])

        return insights
        
    except json.JSONDecodeError:
        # Fallback: try to extract insights from plain text
        return {
            "summary": "Analysis generated",
            "findings": [content[:500]] if content else [],
            "suggestions": []
        }
    except Exception:
        return _empty_insights("Unable to parse insights")


def _empty_insights(message: str, prompt: str = "", system_message: str = "") -> Dict[str, Any]:
    """Return empty insights structure with a message."""
    return {
        "summary": message,
        "findings": [],
        "suggestions": [],
        "prompt": prompt,
        "system_message": system_message
    }


async def generate_insights_stream(
    dataset: Any,
    context: Dict[str, Any],
    original_question: str,
    llm_service: Any = None,
    prompt_template: Optional[str] = None,
    model_override: Any = None,
):
    """Streaming variant of ``generate_insights``.

    Yields a sequence of typed events that the HTTP layer relays as SSE:

    - ``{"type": "open", "prompt": str, "system_message": str}`` (always first)
    - ``{"type": "ttft", "ms": int}`` once the first non-empty content delta arrives
    - ``{"type": "delta", "text": str}`` for each content chunk
    - ``{"type": "done", "insights": dict, "metrics": {input_tokens, output_tokens, total_tokens, llm_latency_ms, ttft_ms}}``
    - ``{"type": "error", "error": str}`` on any failure

    The accumulated text is parsed by the same JSON parser the non-streaming
    path uses, so the final ``insights`` payload matches the existing shape
    consumed by ``InsightsManager`` on the client.
    """
    import time as _time

    system_message = "You are a senior data analyst specialized in finding actionable insights."

    # ----- early-out paths (mirror generate_insights) -----
    try:
        if isinstance(dataset, dict):
            if "rows" in dataset and "columns" in dataset:
                df = pd.DataFrame(dataset["rows"], columns=dataset["columns"])
            else:
                df = pd.DataFrame(dataset)
        elif isinstance(dataset, pd.DataFrame):
            df = dataset
        else:
            yield {
                "type": "done",
                "insights": _empty_insights(
                    "Unsupported dataset format",
                    "N/A - Unsupported dataset format",
                    system_message,
                ),
                "metrics": {"input_tokens": None, "output_tokens": None, "total_tokens": None, "llm_latency_ms": 0, "ttft_ms": None},
            }
            return

        if df.empty:
            yield {
                "type": "done",
                "insights": _empty_insights(
                    "No data returned from query",
                    "N/A - No data returned from query",
                    system_message,
                ),
                "metrics": {"input_tokens": None, "output_tokens": None, "total_tokens": None, "llm_latency_ms": 0, "ttft_ms": None},
            }
            return

        if len(df) == 1:
            yield {
                "type": "done",
                "insights": _empty_insights(
                    "Single record returned, no patterns to analyze",
                    "N/A - Single record, no patterns to analyze",
                    system_message,
                ),
                "metrics": {"input_tokens": None, "output_tokens": None, "total_tokens": None, "llm_latency_ms": 0, "ttft_ms": None},
            }
            return

        prompt = _build_insight_prompt(
            dataset_summary=_prepare_dataset_summary(df),
            context=context,
            original_question=original_question,
            prompt_template=prompt_template,
        )

        if llm_service is None:
            yield {
                "type": "done",
                "insights": _empty_insights("LLM service not available", prompt, system_message),
                "metrics": {"input_tokens": None, "output_tokens": None, "total_tokens": None, "llm_latency_ms": 0, "ttft_ms": None},
            }
            return

        yield {"type": "open", "prompt": prompt, "system_message": system_message}

        # ----- LLM streaming -----
        accumulated = []
        usage: Dict[str, Any] = {}
        ttft_ms = None
        t0 = _time.perf_counter()

        try:
            async for ev in llm_service.generate_streaming(
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1024,
                model_override=model_override,
            ):
                if ev.get("type") == "delta":
                    text = ev.get("text") or ""
                    if text:
                        if ttft_ms is None:
                            ttft_ms = int((_time.perf_counter() - t0) * 1000)
                            yield {"type": "ttft", "ms": ttft_ms}
                        accumulated.append(text)
                        yield {"type": "delta", "text": text}
                elif ev.get("type") == "usage":
                    usage = ev.get("usage") or {}
                elif ev.get("type") == "error":
                    yield {"type": "error", "error": ev.get("error") or "streaming failed"}
                    return
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "error": str(e)}
            return

        llm_latency_ms = int((_time.perf_counter() - t0) * 1000)
        full_text = "".join(accumulated)

        insights = _parse_insights_response(full_text) if full_text else _empty_insights(
            "No content returned", prompt, system_message
        )
        insights["prompt"] = prompt
        insights["system_message"] = system_message

        yield {
            "type": "done",
            "insights": insights,
            "metrics": {
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "llm_latency_ms": llm_latency_ms,
                "ttft_ms": ttft_ms,
            },
        }
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "error": str(e)}


# Async version for use with asyncio
async def generate_insights_async(
    dataset: Any,
    context: Dict[str, Any],
    original_question: str,
    llm_service: Any = None
) -> Dict[str, Any]:
    """
    Async version of generate_insights.
    Calls the synchronous version in a thread pool.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        result = await loop.run_in_executor(
            executor,
            generate_insights,
            dataset,
            context,
            original_question,
            llm_service
        )
    
    return result
