"""Single orchestrator-facing executor composed from bounded domain executors."""

from typing import Any, cast

from app.schemas.ai import ToolCall, ToolDeclaration, ToolResult


class CompositeToolExecutor:
    def __init__(self, *executors: Any) -> None:
        self.executors = executors
        self.declarations: list[ToolDeclaration] = []
        self._routes: dict[str, Any] = {}
        for executor in executors:
            for declaration in executor.declarations:
                if declaration.name in self._routes:
                    raise ValueError(f"duplicate tool declaration: {declaration.name}")
                self.declarations.append(declaration)
                self._routes[declaration.name] = executor

    @property
    def actions(self) -> list[dict[str, Any]]:
        return [action for executor in self.executors for action in executor.actions]

    async def execute(self, call: ToolCall) -> ToolResult:
        executor = self._routes.get(call.name)
        if executor is None:
            return ToolResult(call.name, {"ok": False, "error": "unknown_tool"}, call.call_id)
        return cast(ToolResult, await executor.execute(call))
