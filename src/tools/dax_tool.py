"""DAX tool for the LLM function-calling interface (text-to-DAX).

``RunDaxTool`` is the DAX analogue of :class:`src.tools.sql_tool.RunSqlTool`.
The generator node uses it *schema-only* (no runner bound) so the model emits a
structured ``run_dax`` tool call rather than free-form prose. Actual execution
is done by the ``pbi_execute_query`` node via
:class:`src.connectors.powerbi.PowerBiDaxClient`, which resolves the user's
delegated token at request time — the tool never holds a client or a token.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class RunDaxTool:
    """Tool wrapper describing the ``run_dax`` function for the LLM."""

    def __init__(
        self,
        connection_display_name: Optional[str] = None,
        *,
        dataset_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> None:
        self.name = "run_dax"
        self.dataset_id = (dataset_id or "").strip()
        self.workspace_id = (workspace_id or "").strip()
        if connection_display_name:
            self.description = (
                f"Execute a single read-only DAX query against the "
                f"{connection_display_name} Power BI dataset and return one table."
            )
        else:
            self.description = (
                "Execute a single read-only DAX query against the active Power BI "
                "dataset and return one table."
            )

    def get_schema(self) -> Dict[str, Any]:
        """Return the OpenAI function-calling schema for ``run_dax``."""
        target_parts = []
        if self.workspace_id:
            target_parts.append(f"workspace_id={self.workspace_id}")
        if self.dataset_id:
            target_parts.append(f"dataset_id={self.dataset_id}")
        target_hint = f" Target: {', '.join(target_parts)}." if target_parts else ""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dax": {
                            "type": "string",
                            "description": (
                                "A single read-only DAX query: one top-level "
                                "EVALUATE returning ONE table, optionally preceded "
                                "by a single DEFINE block (MEASURE/VAR only). No "
                                "trailing semicolon, no markdown fences, no MDX or "
                                "DMV, and no multiple EVALUATE statements."
                                + target_hint
                            ),
                        }
                    },
                    "required": ["dax"],
                },
            },
        }


__all__ = ["RunDaxTool"]
