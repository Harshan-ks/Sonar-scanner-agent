# sonar_agent.py
"""
Sonar Scanner Agent

Role: Fetches issues from SonarQube, triages them, and produces a structured,
confidence-scored list of what's worth fixing.

Input:   project key, severity filter, rule whitelist (fix_rules.yaml)
Process: call SonarQube REST API -> filter by severity -> filter by rule whitelist
         -> group by file -> score each issue by fix confidence
Output:  output/issues.json -> [{file, line, rule, message, severity, type,
         confidence, fan_in}, ...]

Confidence scoring combines two signals (see Approach_confidence_score.txt
for the full write-up):
  1. Rule whitelist scoring - is this TYPE of issue safe to touch? Presence
     on fix_rules.yaml is a hard filter (issues off the list never reach
     scoring at all); type/severity then adjust the score up or down.
  2. Dependency fan-in scoring - is this FILE safe to touch? A file many
     others import is riskier to change than an isolated one, regardless of
     how safe the rule itself is. Fan-in comes from dependency_graph.json
     (built by dependency_graph.py, Python-only - see that file's docstring
     for the language limitation). If no graph is available, or a file has
     no entry in it, the fan-in adjustment is neutral (0.0).

Optional LLM rescue (SONAR_LLM_RESCUE=true, off by default): an issue whose
rule ISN'T on the whitelist is normally dropped outright. When rescue is
enabled and a project_root is given, non-whitelisted CODE_SMELL issues get
one read-only Claude Code call asking whether this specific instance is a
safe, mechanical fix despite the rule never being pre-vetted - catching
valid candidates a static list would otherwise miss. Rescued issues are
marked llm_rescued: true and scored with a smaller safety bonus (0.15 vs
0.30) than a human-vetted whitelist match, reflecting the lower certainty.
BUG and VULNERABILITY issues are never offered for rescue.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

import claude_cli

load_dotenv()

SONAR_URL = os.environ.get("SONAR_URL", "http://localhost:9000")
SONAR_TOKEN = os.environ["SONAR_TOKEN"]
NEW_CODE_ONLY = os.environ.get("SONAR_NEW_CODE_ONLY", "true").lower() == "true"
LLM_RESCUE_ENABLED = os.environ.get("SONAR_LLM_RESCUE", "false").lower() == "true"

BASE_DIR = Path(__file__).parent
RULES_FILE = BASE_DIR / "fix_rules.yaml"
DEPENDENCY_GRAPH_FILE = BASE_DIR / "dependency_graph.json"
OUTPUT_DIR = BASE_DIR / "output"

DEFAULT_SEVERITIES = ["BLOCKER", "CRITICAL", "MAJOR"]


def load_rule_whitelist():
    with open(RULES_FILE) as f:
        data = yaml.safe_load(f)
    return set(data["rules"])


def load_dependency_graph():
    if not DEPENDENCY_GRAPH_FILE.exists():
        return {}
    with open(DEPENDENCY_GRAPH_FILE) as f:
        return json.load(f)


def rule_matches_whitelist(rule_key, whitelist):
    """fix_rules.yaml stores bare rule numbers (e.g. "S1481"), not full
    "<language>:<rule>" keys, since SonarSource reuses the same number
    across languages for equivalent checks. Matching is against everything
    after the last ":" in the issue's actual rule key."""
    _, _, number = rule_key.rpartition(":")
    return number in whitelist


def fetch_issues(project_key, severities):
    """Calls GET /api/issues/search with server-side severity filtering.
    Rule filtering can't happen server-side anymore: fix_rules.yaml stores
    language-agnostic rule numbers, but the API's `rules` param needs full
    "<language>:<rule>" keys. So every severity-matching issue is fetched,
    and the whitelist check happens client-side in run()."""
    params = {
        "components": project_key,
        "severities": ",".join(severities),
        "statuses": "OPEN,CONFIRMED,REOPENED",
        "ps": 500,
    }
    if NEW_CODE_ONLY:
        # Quality gates evaluate New Code by default, so scope the agent to the
        # same issues that actually caused the gate to fail.
        params["inNewCodePeriod"] = "true"

    issues = []
    page = 1
    while True:
        params["p"] = page
        resp = requests.get(
            f"{SONAR_URL}/api/issues/search",
            params=params,
            auth=(SONAR_TOKEN, ""),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data["issues"])
        fetched = page * data["paging"]["pageSize"]
        if fetched >= data["paging"]["total"]:
            break
        page += 1
    return issues


def fan_in_adjustment(file_rel_path, dependency_graph):
    entry = dependency_graph.get(file_rel_path)
    if entry is None:
        return 0.0  # no data for this file (not Python, or graph not built) -> neutral
    fan_in = entry["fan_in"]
    if fan_in == 0:
        return 0.10
    if fan_in <= 2:
        return 0.0
    if fan_in <= 5:
        return -0.10
    return -0.20


def build_rescue_prompt(file_rel_path, issue):
    return (
        f'A SonarQube rule that is NOT on our pre-vetted whitelist flagged '
        f'this issue in "{file_rel_path}":\n\n'
        f"- Line {issue.get('line')}, rule {issue['rule']} "
        f"({issue['severity']}, type {issue.get('type')}): {issue['message']}\n\n"
        "Read the file and decide whether THIS SPECIFIC instance is safe for "
        "an automated tool to fix with a small, mechanical, low-risk change - "
        "or whether it needs human judgment (e.g. touches business logic, "
        "security, or requires understanding intent beyond the local code).\n\n"
        "Respond with EXACTLY one line in this format, nothing else:\n"
        "VERDICT: RESCUE - <one sentence reason>\n"
        "or\n"
        "VERDICT: SKIP - <one sentence reason>"
    )


def rescue_review(project_root, file_rel_path, issue):
    """Read-only Claude Code call for an issue whose rule isn't whitelisted.
    Fails closed: any error, timeout, or unparseable response is treated as
    SKIP so a broken review never silently rescues everything."""
    prompt = build_rescue_prompt(file_rel_path, issue)
    try:
        response = claude_cli.run_claude(prompt, cwd=project_root, allowed_tools="Read")
    except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as e:
        return False, f"rescue review call failed: {e}"

    if response.get("is_error"):
        return False, f"rescue review reported an error: {response.get('result')}"

    rescue, reason = claude_cli.parse_verdict(response.get("result"), "RESCUE")
    if rescue is None:
        return False, reason
    return rescue, reason


def score_confidence(issue, file_rel_path, dependency_graph, whitelisted=True):
    # Base + safety-source bonus: a human-vetted whitelist match gets the
    # full documented bonus; an LLM-rescued issue (rule not pre-vetted, but
    # judged safe for this specific instance) gets half, reflecting the
    # lower certainty of an automated judgment call vs. a curated list.
    score = 0.5 + (0.30 if whitelisted else 0.15)

    issue_type = issue.get("type")
    if issue_type == "CODE_SMELL":
        score += 0.10
    elif issue_type == "BUG":
        score -= 0.10
    elif issue_type == "VULNERABILITY":
        score -= 0.20

    if issue.get("severity") == "BLOCKER":
        score -= 0.05

    score += fan_in_adjustment(file_rel_path, dependency_graph)

    return round(max(0.0, min(1.0, score)), 2)


def run(project_key, severities=None, project_root=None):
    severities = severities or DEFAULT_SEVERITIES
    whitelist = load_rule_whitelist()
    dependency_graph = load_dependency_graph()
    rescue_active = LLM_RESCUE_ENABLED and project_root is not None

    print(f"[sonar_agent] Fetching issues for '{project_key}' "
          f"(severities={severities}, {len(whitelist)} whitelisted rules, "
          f"new_code_only={NEW_CODE_ONLY}, "
          f"dependency_graph={'loaded (' + str(len(dependency_graph)) + ' files)' if dependency_graph else 'not found'}, "
          f"llm_rescue={'on' if rescue_active else ('off' if not LLM_RESCUE_ENABLED else 'off (no project_root)')})")

    raw_issues = fetch_issues(project_key, severities)

    results = []
    for issue in raw_issues:
        # component is "<project_key>:<file_path>", but project_key itself may
        # contain colons (e.g. Maven's "groupId:artifactId"), so strip it by
        # known length rather than splitting on the first ":".
        component = issue["component"]
        prefix = project_key + ":"
        file_path = component[len(prefix):] if component.startswith(prefix) else component

        whitelisted = rule_matches_whitelist(issue["rule"], whitelist)
        rescue_reason = None

        if not whitelisted:
            if not (rescue_active and issue.get("type") == "CODE_SMELL"):
                continue
            rescued, rescue_reason = rescue_review(project_root, file_path, issue)
            if not rescued:
                print(f"[sonar_agent]   not rescued: {issue['rule']} in {file_path} - {rescue_reason}")
                continue
            print(f"[sonar_agent]   rescued: {issue['rule']} in {file_path} - {rescue_reason}")

        entry = {
            "file": file_path,
            "line": issue.get("line"),
            "rule": issue["rule"],
            "message": issue["message"],
            "severity": issue["severity"],
            "type": issue.get("type"),
            "confidence": score_confidence(issue, file_path, dependency_graph, whitelisted=whitelisted),
            "fan_in": dependency_graph.get(file_path, {}).get("fan_in"),
        }
        if not whitelisted:
            entry["llm_rescued"] = True
            entry["rescue_reason"] = rescue_reason

        results.append(entry)

    # Highest-confidence, easiest wins first
    results.sort(key=lambda x: x["confidence"], reverse=True)

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / "issues.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"[sonar_agent] {len(results)} issue(s) survived filtering -> {output_path}")
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python sonar_agent.py <project_key> [SEVERITY1,SEVERITY2,...] [project_root]")
        sys.exit(1)

    key = sys.argv[1]
    sevs = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    root = sys.argv[3] if len(sys.argv) > 3 else None
    run(key, sevs, project_root=root)
