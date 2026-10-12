# sonar_mcp.py
"""
MCP-routed SonarQube lookups: project discovery and project-wide health
metrics.

Deliberately routed through the SonarQube MCP server (via a scoped Claude
Code call) rather than a plain REST call, per explicit design choice - all
three equivalent SonarQube endpoints (/api/projects/search,
/api/measures/component, /api/metrics/search) could be called directly and
for free, but this project chose to route them through Claude + MCP
instead. That means every call here is a real LLM call with real cost, not
a free REST fetch - worth keeping in mind before calling these in a loop.

get_component_measures is project-WIDE, not per-file: it cannot replace or
complement sonar_agent.py's per-file fan-in signal, which specifically
needs file-level granularity. It's a coarser, whole-project health signal
instead (see project_measures_adjustment() in sonar_agent.py).

Used by:
  - main.py's `projects`, `measures`, `metrics` subcommands (direct lookups)
  - main.py's `scan-all` (project discovery, to loop the pipeline across
    every project with a known local root in projects.yaml)
  - sonar_agent.py (optional project-wide measures at the start of a run,
    gated by SONAR_FETCH_PROJECT_MEASURES)
"""
import json
import subprocess
from pathlib import Path

import claude_cli

BASE_DIR = Path(__file__).parent

DEFAULT_METRIC_KEYS = ["coverage", "complexity", "duplicated_lines_density"]

MCP_SEARCH_PROJECTS = "mcp__sonarqube__search_my_sonarqube_projects"
MCP_GET_MEASURES = "mcp__sonarqube__get_component_measures"
MCP_SEARCH_METRICS = "mcp__sonarqube__search_metrics"


def build_discovery_prompt():
    return (
        "Use the search_my_sonarqube_projects tool to list every SonarQube "
        "project you have access to.\n\n"
        "Respond with EXACTLY one line per project, nothing else, in this format:\n"
        "PROJECT: <key> | <name>"
    )


def discover_projects(cwd=None):
    """Returns [{"key", "name"}, ...] for every project the MCP server can see."""
    prompt = build_discovery_prompt()
    response = claude_cli.run_claude(prompt, cwd=cwd or BASE_DIR, allowed_tools=MCP_SEARCH_PROJECTS)
    text = response.get("result") or ""
    projects = []
    for line in claude_cli.parse_all_fields(text, "PROJECT"):
        key, _, name = line.partition("|")
        projects.append({"key": key.strip(), "name": name.strip() or key.strip()})
    return projects


def build_measures_prompt(project_key, metric_keys):
    keys_str = ", ".join(metric_keys)
    return (
        f'Use the get_component_measures tool for projectKey "{project_key}" '
        f"with metricKeys [{keys_str}].\n\n"
        "Respond with EXACTLY one line per metric, nothing else, in this format:\n"
        "METRIC: <key> = <value>"
    )


def fetch_project_measures(project_key, metric_keys=None, cwd=None):
    """Project-WIDE measures (not per-file). Returns {} on any failure - a
    missing signal should never block the rest of the pipeline."""
    metric_keys = metric_keys or DEFAULT_METRIC_KEYS
    prompt = build_measures_prompt(project_key, metric_keys)
    try:
        response = claude_cli.run_claude(prompt, cwd=cwd or BASE_DIR, allowed_tools=MCP_GET_MEASURES)
    except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as e:
        print(f"[sonar_mcp] fetch_project_measures failed for {project_key}: {e}")
        return {}
    if response.get("is_error"):
        print(f"[sonar_mcp] fetch_project_measures reported an error for "
              f"{project_key}: {response.get('result')}")
        return {}
    text = response.get("result") or ""
    measures = {}
    for line in claude_cli.parse_all_fields(text, "METRIC"):
        key, _, value = line.partition("=")
        measures[key.strip()] = value.strip()
    return measures


def build_list_metrics_prompt():
    return (
        "Use the search_metrics tool to list available SonarQube metrics.\n\n"
        "Respond with EXACTLY one line per metric, nothing else, in this format:\n"
        "METRIC_KEY: <key> | <name>"
    )


def list_available_metrics(cwd=None):
    """One-time reference lookup (which metric keys exist) - not meant to
    be called on every pipeline run, unlike fetch_project_measures."""
    prompt = build_list_metrics_prompt()
    response = claude_cli.run_claude(prompt, cwd=cwd or BASE_DIR, allowed_tools=MCP_SEARCH_METRICS)
    text = response.get("result") or ""
    metrics = []
    for line in claude_cli.parse_all_fields(text, "METRIC_KEY"):
        key, _, name = line.partition("|")
        metrics.append({"key": key.strip(), "name": name.strip() or key.strip()})
    return metrics


def build_rule_description_prompt(rule_key):
    return (
        f"Use the show_rule tool (key: {rule_key}) to look up this SonarQube rule. "
        "Then respond with ONLY the rule's complete description: its full "
        "rationale (why this matters), and any compliant/non-compliant code "
        "examples it provides. Do not add any commentary, preamble, or "
        "meta-text - just the rule's content itself, reproduced in full."
    )


def fetch_rule_description(rule_key, cwd=None):
    """Full rule rationale + compliant/non-compliant examples, as opposed to
    show_rule's use elsewhere (informing a single reasoning turn without the
    content itself ever reaching our code). Deliberately NOT squeezed into a
    single-line "LABEL: value" field like the other functions here - rule
    descriptions are often multi-paragraph with embedded code blocks, so the
    raw response text IS the description. Returns None on any failure; a
    missing description should never block the rest of the pipeline."""
    prompt = build_rule_description_prompt(rule_key)
    try:
        response = claude_cli.run_claude(prompt, cwd=cwd or BASE_DIR, allowed_tools=claude_cli.MCP_SHOW_RULE)
    except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as e:
        print(f"[sonar_mcp] fetch_rule_description failed for {rule_key}: {e}")
        return None
    if response.get("is_error"):
        print(f"[sonar_mcp] fetch_rule_description reported an error for "
              f"{rule_key}: {response.get('result')}")
        return None
    text = (response.get("result") or "").strip()
    return text or None
