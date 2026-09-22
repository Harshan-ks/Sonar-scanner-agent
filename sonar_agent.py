# sonar_agent.py
"""
Sonar Scanner Agent

Role: Fetches issues from SonarQube, triages them, and produces a structured,
confidence-scored list of what's worth fixing. Every issue that matches the
severity filter gets written to output/issues.json - not just the fixable
ones. Anything not eligible for an automatic fix is marked
needs_human_judgment: true and carries a probable_fix suggestion, so a human
always has a concrete starting point instead of just being told "skipped."

Input:   project key, severity filter, rule whitelist (fix_rules.yaml)
Process: call SonarQube REST API -> filter by severity -> for each issue,
         decide whitelisted / rescued / needs-human-judgment -> group by
         file -> score each issue by fix confidence
Output:  output/issues.json -> [{file, line, rule, message, severity, type,
         confidence, fan_in, needs_human_judgment, ...}, ...]

Confidence scoring combines two signals (see Approach_confidence_score.txt
for the full write-up):
  1. Rule whitelist scoring - is this TYPE of issue safe to touch? Presence
     on fix_rules.yaml is a hard filter for automatic fixing (though not for
     being recorded at all - see above); type/severity then adjust the
     score up or down.
  2. Dependency fan-in scoring - is this FILE safe to touch? A file many
     others import is riskier to change than an isolated one, regardless of
     how safe the rule itself is. Fan-in comes from dependency_graph.json
     (built by dependency_graph.py, Python-only - see that file's docstring
     for the language limitation). If no graph is available, or a file has
     no entry in it, the fan-in adjustment is neutral (0.0).

Optional LLM rescue (SONAR_LLM_RESCUE=true, off by default): an issue whose
rule ISN'T on the whitelist would otherwise go straight to human judgment.
When rescue is enabled and a project_root is given, non-whitelisted
CODE_SMELL issues get one read-only Claude Code call asking whether this
specific instance is a safe, mechanical fix despite the rule never being
pre-vetted - catching valid candidates a static list would otherwise miss.
That same call always returns a probable_fix too, so a SKIP verdict still
carries a concrete suggestion. Rescued issues are marked llm_rescued: true
and scored with a smaller safety bonus (0.15 vs 0.30) than a human-vetted
whitelist match, reflecting the lower certainty. BUG and VULNERABILITY
issues are never offered for rescue.
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
        "Respond with EXACTLY these two lines, nothing else:\n"
        "VERDICT: RESCUE - <one sentence reason>  (use SKIP - <reason> if it needs human judgment)\n"
        "PROBABLE_FIX: <the concrete change that resolves this issue, whether or not it's rescued>"
    )


def rescue_review(project_root, file_rel_path, issue):
    """Read-only Claude Code call for an issue whose rule isn't whitelisted.
    Fails closed: any error, timeout, or unparseable response is treated as
    SKIP so a broken review never silently rescues everything. Always
    returns a probable_fix suggestion regardless of verdict."""
    prompt = build_rescue_prompt(file_rel_path, issue)
    try:
        response = claude_cli.run_claude(prompt, cwd=project_root, allowed_tools="Read")
    except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as e:
        return False, f"rescue review call failed: {e}", issue["message"]

    text = response.get("result")
    if response.get("is_error"):
        return False, f"rescue review reported an error: {text}", issue["message"]

    rescued, reason = claude_cli.parse_verdict(text, "RESCUE")
    probable_fix = claude_cli.parse_probable_fix(text, issue["message"])
    if rescued is None:
        return False, reason, probable_fix
    return rescued, reason, probable_fix


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

        entry = {
            "file": file_path,
            "line": issue.get("line"),
            "rule": issue["rule"],
            "message": issue["message"],
            "severity": issue["severity"],
            "type": issue.get("type"),
        }

        if whitelisted:
            entry["needs_human_judgment"] = False
            entry["confidence"] = score_confidence(issue, file_path, dependency_graph, whitelisted=True)
            entry["fan_in"] = dependency_graph.get(file_path, {}).get("fan_in")
            results.append(entry)
            continue

        rescue_eligible = rescue_active and issue.get("type") == "CODE_SMELL"
        rescued = False
        reason = None
        probable_fix = None

        if rescue_eligible:
            rescued, reason, probable_fix = rescue_review(project_root, file_path, issue)
            if rescued:
                print(f"[sonar_agent]   rescued: {issue['rule']} in {file_path} - {reason}")
            else:
                print(f"[sonar_agent]   not rescued: {issue['rule']} in {file_path} - {reason}")
        else:
            reason = ("rule not whitelisted, and not eligible for rescue "
                      f"(type={issue.get('type')}, rescue_enabled={LLM_RESCUE_ENABLED})")
            probable_fix = claude_cli.suggest_probable_fix(project_root, file_path, issue)

        entry["confidence"] = score_confidence(issue, file_path, dependency_graph, whitelisted=False)
        entry["fan_in"] = dependency_graph.get(file_path, {}).get("fan_in")

        if rescued:
            entry["needs_human_judgment"] = False
            entry["llm_rescued"] = True
            entry["rescue_reason"] = reason
        else:
            entry["needs_human_judgment"] = True
            entry["reason"] = reason
            entry["probable_fix"] = probable_fix

        results.append(entry)

    # Highest-confidence, easiest wins first
    results.sort(key=lambda x: x["confidence"], reverse=True)

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / "issues.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    human_count = sum(1 for r in results if r["needs_human_judgment"])
    print(f"[sonar_agent] {len(results)} issue(s) written -> {output_path} "
          f"({len(results) - human_count} fixable, {human_count} need human judgment)")
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python sonar_agent.py <project_key> [SEVERITY1,SEVERITY2,...] [project_root]")
        sys.exit(1)

    key = sys.argv[1]
    sevs = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    root = sys.argv[3] if len(sys.argv) > 3 else None
    run(key, sevs, project_root=root)
