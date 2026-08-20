"""MCP + Python Tool System.

Two-tier architecture:
  ToolKit     — a named collection of related tools (can be MCP or Python)
  ToolRegistry — per-role tool registry that manages ToolKits

Supports:
  - Python-native tools: handler = callable(dict) → str
  - MCP tools: compatible with mcp.types.Tool + stdio_client
  - Duplicate detection: warns when two toolkits register the same tool name
  - Role can add a full toolkit at once via AgentRole.add_toolkit()

接口文档 (模块结构与方法):

类与方法:
    ToolDef: (枚举/常量类)
    ToolKit:
        - add_python_tool(): Add a Python-native tool to this toolkit.
        - tool_names(): 见方法源码
        - tool_count(): 见方法源码
        - get_tool(): 见方法源码
    ToolRegistry:
        - add_toolkit(): Import an entire toolkit. Returns number of new tools added.
        - remove_toolkit(): Remove a toolkit and all its tools. Returns number of tools removed.
        - add_tool(): Register a single Python tool.
        - remove_tool(): 见方法源码
        - list_tools(): Return all tools in LLM-compatible format.
        - call_tool(): Execute a tool by name. Searches all loaded toolkits.
        - to_openai_tools(): 生成 OpenAI 原生 function calling 格式的工具声明列表.
        - tool_names(): 见方法源码
        - toolkit_names(): 见方法源码
        - tool_count(): 见方法源码
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    from mcp.types import CallToolResult, TextContent, Tool as MCPTool
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

    @dataclass
    class MCPTool:
        name: str
        description: str | None = None
        inputSchema: dict[str, Any] = field(default_factory=dict)
        title: str | None = None

    @dataclass
    class TextContent:
        type: str = "text"
        text: str = ""

    @dataclass
    class CallToolResult:
        content: list[TextContent] = field(default_factory=list)
        is_error: bool = False

logger = logging.getLogger(__name__)

# ── Tool Handler ──────────────────────────────────────────

ToolHandler = Callable[[dict[str, Any]], str]
"""A tool handler receives arguments dict and returns a string result."""


# ── ToolDef (unified) ─────────────────────────────────────

@dataclass
class ToolDef:
    """Unified tool definition — works for both MCP and Python-native tools."""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    handler: Optional[ToolHandler] = None          # Python-native handler
    source: str = "python"                         # "python" | "mcp" | "talk"
    mcp_tool: Any = None                           # Original MCP Tool object if applicable


# ── ToolKit ───────────────────────────────────────────────

class ToolKit:
    """A named collection of related tools.

    Can contain:
      - Python-native tools (handler = callable)
      - MCP-based tools (loaded from an MCP server)
      - Mixed (some Python, some MCP)

    Usage:
        # Python toolkit
        coding = ToolKit(name="coding", description="File and code operations")
        coding.add_python_tool("read_file", "Read a file", {...}, handler)

        # Role imports the whole toolkit
        role.add_toolkit(coding)
    """

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._tools: dict[str, ToolDef] = {}

    # ── Python tool management ────────────────────────────

    def add_python_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
    ) -> ToolDef:
        """Add a Python-native tool to this toolkit."""
        if name in self._tools:
            raise ValueError(f"Tool '{name}' already exists in toolkit '{self.name}'")
        td = ToolDef(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            source="python",
        )
        self._tools[name] = td
        logger.info("ToolKit[%s] Python tool: %s", self.name, name)
        return td

    # ── Properties ────────────────────────────────────────

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def get_tool(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def __iter__(self):
        return iter(self._tools.values())

    def __contains__(self, name: str) -> bool:
        return name in self._tools


class ToolRegistry:
    """Per-role tool registry that manages ToolKits.

    Supports:
      - Adding individual Python tools (single-tool registration)
      - Adding entire ToolKits at once (with duplicate detection)
      - Listing all tools across all loaded toolkits for LLM context
      - Executing tools by name (searches all toolkits)

    Usage:
        reg = ToolRegistry()
        tk = ToolKit(name="my_tools", description="...")
        tk.add_python_tool("hello", "Say hello", {"type": "object", "properties": {}},
                           handler=lambda a: "hi")
        reg.add_toolkit(tk)
        # Duplicate detection: warns if two toolkits register same name
        reg.call_tool("hello", {})
        # 完整"如何写 ToolKit"示例见 src/python_tools/examples/example_toolkits.py
    """

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}          # unified registry
        self._toolkits: dict[str, ToolKit] = {}       # loaded toolkits by name
        # Track which toolkit each tool came from
        self._tool_source: dict[str, str] = {}        # tool_name → toolkit_name

    # ── Toolkit management ────────────────────────────────

    def add_toolkit(self, toolkit: ToolKit) -> int:
        """Import an entire toolkit. Returns number of new tools added.

        If a tool with the same name already exists, it is skipped with a warning.
        """
        if toolkit.name in self._toolkits:
            logger.warning("Toolkit '%s' already loaded, skipping", toolkit.name)
            return 0

        self._toolkits[toolkit.name] = toolkit
        added = 0

        for td in toolkit:
            if td.name in self._tools:
                existing = self._tool_source.get(td.name, "unknown")
                logger.warning(
                    "Tool '%s' from toolkit '%s' conflicts with existing tool from '%s' — keeping original",
                    td.name, toolkit.name, existing,
                )
                continue
            self._tools[td.name] = td
            self._tool_source[td.name] = toolkit.name
            added += 1

        logger.info(
            "ToolRegistry: loaded toolkit '%s' — %d tools (%d new, %d skipped)",
            toolkit.name, toolkit.tool_count, added, toolkit.tool_count - added,
        )
        return added

    def remove_toolkit(self, name: str) -> int:
        """Remove a toolkit and all its tools. Returns number of tools removed."""
        if name not in self._toolkits:
            return 0
        tk = self._toolkits.pop(name)
        removed = 0
        for td in tk:
            if self._tool_source.get(td.name) == name:
                self._tools.pop(td.name, None)
                self._tool_source.pop(td.name, None)
                removed += 1
        return removed

    # ── Single tool management ────────────────────────

    def add_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
        source: str = "inline",
    ) -> None:
        """Register a single Python tool."""
        if name in self._tools:
            logger.warning("Tool '%s' already registered, overwriting", name)
        td = ToolDef(name=name, description=description, input_schema=input_schema,
                     handler=handler, source=source)
        self._tools[name] = td
        self._tool_source[name] = source
        logger.info("Tool registered: %s — %s", name, description[:60])

    def remove_tool(self, name: str) -> None:
        self._tools.pop(name, None)
        self._tool_source.pop(name, None)

    # ── MCP Protocol Methods ──────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all tools in LLM-compatible format."""
        return [
            {
                "name": td.name,
                "description": td.description,
                "input_schema": td.input_schema,
            }
            for td in self._tools.values()
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Execute a tool by name. Searches all loaded toolkits."""
        td = self._tools.get(name)
        if td is None:
            return CallToolResult(
                content=[TextContent(text=f"Error: tool '{name}' not found. Available: {list(self._tools)}")],
                is_error=True,
            )

        if td.handler is None:
            # MCP tool — would need session.call_tool(), but we don't keep sessions
            return CallToolResult(
                content=[TextContent(text=f"Error: tool '{name}' is MCP-based and requires an active server connection")],
                is_error=True,
            )

        try:
            result_text = td.handler(arguments)
            return CallToolResult(
                content=[TextContent(text=str(result_text))],
                is_error=False,
            )
        except Exception as exc:
            logger.exception("Tool '%s' execution failed", name)
            return CallToolResult(
                content=[TextContent(text=f"Tool error: {exc}")],
                is_error=True,
            )

    def to_openai_tools(self) -> list[dict]:
        """生成 OpenAI 原生 function calling 格式的工具声明列表.

        返回:
            [{"type": "function", "function": {"name", "description", "parameters"}}]
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"] or {"type": "object", "properties": {}},
                },
            }
            for t in self.list_tools()
        ]

    # ── Properties ────────────────────────────────────────

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    @property
    def toolkit_names(self) -> list[str]:
        return list(self._toolkits)

    @property
    def tool_count(self) -> int:
        return len(self._tools)
