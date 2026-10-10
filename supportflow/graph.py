"""Diagnostic state snapshots. Business recovery is owned by the business database."""

import sqlite3
from pathlib import Path
from typing import Any, TypedDict


class WorkflowState(TypedDict, total=False):
    task_id: str
    customer_id: str
    status: str
    intent: str
    order_id: str | None
    pending_action: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None
    updated_at: str | None
    context: dict[str, Any]


class CheckpointManager:
    """Persist the task execution envelope using LangGraph's SQLite checkpointer."""

    def __init__(self, path: str):
        from langgraph.checkpoint.sqlite import SqliteSaver

        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        # Keep the constructor compatible with langgraph-checkpoint-sqlite 2.x.
        self.saver = SqliteSaver(self.connection)
        self.saver.setup()
        self.graph = self._build_checkpoint_graph()

    def _build_checkpoint_graph(self):
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(WorkflowState)
        graph.add_node("persist_state", lambda state: state)
        graph.add_edge(START, "persist_state")
        graph.add_edge("persist_state", END)
        return graph.compile(checkpointer=self.saver)

    def save(self, state: WorkflowState) -> None:
        self.graph.invoke(state, {"configurable": {"thread_id": state["task_id"]}})

    def load(self, task_id: str) -> WorkflowState | None:
        snapshot = self.graph.get_state({"configurable": {"thread_id": task_id}})
        return dict(snapshot.values) if snapshot and snapshot.values else None

    def close(self) -> None:
        self.connection.close()
