"""LangGraph state envelope and SQLite checkpoint integration."""

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


class CheckpointManager:
    """Persist the task execution envelope using LangGraph's SQLite checkpointer."""

    def __init__(self, path: str):
        from langgraph.checkpoint.sqlite import SqliteSaver

        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
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


def build_graph(checkpoint_path: str | None = None):
    if checkpoint_path:
        return CheckpointManager(checkpoint_path).graph
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return None

    graph = StateGraph(WorkflowState)
    graph.add_node("supervisor", lambda state: state)
    graph.add_node("order_agent", lambda state: state)
    graph.add_node("after_sales_agent", lambda state: state)
    graph.add_node("finish", lambda state: state)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", lambda state: state.get("intent", "unknown"), {"query_order": "order_agent", "cancel_order": "order_agent", "create_ticket": "order_agent", "unknown": "finish"})
    graph.add_edge("order_agent", "after_sales_agent")
    graph.add_edge("after_sales_agent", "finish")
    graph.add_edge("finish", END)
    return graph.compile()
