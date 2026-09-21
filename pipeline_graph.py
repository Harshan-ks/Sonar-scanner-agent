# pipeline_graph.py
"""
LangGraph pipeline that orchestrates the Sonar Scanner Agent and the Fix
Agent. This is the actual orchestrator: both the webhook listener and
`main.py run` invoke this compiled graph rather than calling the two agents
by hand.

Graph:
    START -> scan --route--> fix -> END
                        (or straight to END if auto_fix is off, or nothing
                         met fix_agent's confidence threshold)

Neither node calls a model directly - they just call sonar_agent.run() and
fix_agent.run(). fix_agent.run() is the only place a model gets invoked, and
it does so by shelling out to the Claude Code CLI (see fix_agent.py) rather
than through langchain_anthropic, since that path hit persistent rate limits
on the CLAUDE_CODE_OAUTH_TOKEN.
"""
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

import fix_agent
import sonar_agent


class PipelineState(TypedDict):
    project_key: str
    project_root: Optional[str]
    auto_fix: bool
    issues: list
    fix_report: Optional[dict]


def scan_node(state: PipelineState) -> PipelineState:
    issues = sonar_agent.run(state["project_key"], project_root=state.get("project_root"))
    return {**state, "issues": issues}


def fix_node(state: PipelineState) -> PipelineState:
    report = fix_agent.run(state["project_root"])
    return {**state, "fix_report": report}


def route_after_scan(state: PipelineState) -> str:
    if not state.get("auto_fix"):
        print("[pipeline] auto_fix is off -> stopping after issues.json")
        return END
    if not state.get("project_root"):
        print("[pipeline] auto_fix is on but no project_root is set -> skipping fix")
        return END
    if not any(i.get("confidence", 0) >= fix_agent.CONFIDENCE_THRESHOLD for i in state["issues"]):
        print(f"[pipeline] no issue meets the confidence threshold "
              f"({fix_agent.CONFIDENCE_THRESHOLD}) -> nothing for the Fix Agent to do")
        return END
    return "fix"


def build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node("scan", scan_node)
    graph.add_node("fix", fix_node)
    graph.set_entry_point("scan")
    graph.add_conditional_edges("scan", route_after_scan, {"fix": "fix", END: END})
    graph.add_edge("fix", END)
    return graph.compile()


pipeline = build_graph()


def run_pipeline(project_key, project_root=None, auto_fix=False):
    initial_state: PipelineState = {
        "project_key": project_key,
        "project_root": project_root,
        "auto_fix": auto_fix,
        "issues": [],
        "fix_report": None,
    }
    return pipeline.invoke(initial_state)
