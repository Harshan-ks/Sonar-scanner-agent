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
Output:  output/issues.json -> {generated_at, issues: [{issue_key, file, line,
         rule, message, severity, type, confidence, fan_in,
         needs_human_judgment, ...}, ...]}

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
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

import claude_cli
import sonar_mcp

load_dotenv()

SONAR_URL = os.environ.get("SONAR_URL", "http://localhost:9000")
SONAR_TOKEN = os.environ["SONAR_TOKEN"]
NEW_CODE_ONLY = os.environ.get("SONAR_NEW_CODE_ONLY", "true").lower() == "true"
LLM_RESCUE_ENABLED = os.environ.get("SONAR_LLM_RESCUE", "false").lower() == "true"
FETCH_PROJECT_MEASURES = os.environ.get("SONAR_FETCH_PROJECT_MEASURES", "true").lower() == "true"
FETCH_RULE_DESCRIPTIONS = os.environ.get("SONAR_FETCH_RULE_DESCRIPTIONS", "true").lower() == "true"

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


def get_rule_description(rule_key, cache, project_root):
    """Cached per rule KEY (not per issue) within one run() call, so 5
    issues sharing the same rule only trigger one fetch_rule_description
    call, not 5. cache is a plain dict the caller owns and passes in."""
    if not FETCH_RULE_DESCRIPTIONS:
        return None
    if rule_key not in cache:
        cache[rule_key] = sonar_mcp.fetch_rule_description(rule_key, cwd=project_root)
    return cache[rule_key]


def project_measures_adjustment(project_measures):
    """Coarse, project-WIDE confidence adjustment from overall coverage -
    separate from and additive to the per-file fan_in_adjustment above. Low
    overall coverage means a wrong fix ANYWHERE in this project is less
    likely to be caught by tests; high overall coverage means the opposite.
    Unlike fan-in, this is the same value for every issue in a given run,
    since get_component_measures is project-level, not per-file (see
    sonar_mcp.py's docstring)."""
    coverage_str = project_measures.get("coverage")
    if coverage_str is None:
        return 0.0
    try:
        coverage = float(coverage_str)
    except ValueError:
        return 0.0
    if coverage < 20:
        return -0.05
    if coverage >= 80:
        return 0.05
    return 0.0


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


def build_rescue_prompt_batch(file_rel_path, issues):
    issue_lines = "\n".join(
        f"{idx}. Line {i.get('line')}, rule {i['rule']} ({i['severity']}, type {i.get('type')}): {i['message']}"
        for idx, i in enumerate(issues, start=1)
    )
    return (
        f'The following SonarQube issue(s) in "{file_rel_path}" are flagged by '
        f"rules that are NOT on our pre-vetted whitelist:\n\n{issue_lines}\n\n"
        "For EACH issue, use the show_rule tool (key: that issue's rule id) to "
        "see the full rule rationale and compliant/non-compliant examples, "
        "then read the file and decide whether THIS SPECIFIC instance is safe "
        "for an automated tool to fix with a small, mechanical, low-risk "
        "change - or whether it needs human judgment (e.g. touches business "
        "logic, security, or requires understanding intent beyond the local "
        "code).\n\n"
        "Respond with EXACTLY one verdict block per issue above, numbered to "
        "match, nothing else:\n"
        "ISSUE 1: RESCUE - <one sentence reason>  (use SKIP - <reason> if it needs human judgment)\n"
        "PROBABLE_FIX 1: <the concrete change that resolves this issue, whether or not it's rescued>\n"
        "(repeat the ISSUE N / PROBABLE_FIX N pair for every issue number above)"
    )


def rescue_review_batch(project_root, file_rel_path, issues):
    """Like a per-issue rescue review, but covers every rescue-eligible issue
    in one file with a single Claude Code call instead of one call per issue
    - the same per-file batching fix_agent.py already uses for review/fix.
    Fails closed PER ISSUE: any error, timeout, or a verdict this specific
    issue's number can't be parsed out of is treated as SKIP for that issue,
    without discarding verdicts the response did parse for the others.
    Always returns one (rescued, reason, probable_fix) tuple per input issue,
    in the same order."""
    prompt = build_rescue_prompt_batch(file_rel_path, issues)
    try:
        response = claude_cli.run_claude(
            prompt, cwd=project_root, allowed_tools=f"Read {claude_cli.MCP_SHOW_RULE}",
        )
    except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as e:
        return [(False, f"rescue review call failed: {e}", i["message"]) for i in issues]

    text = response.get("result")
    if response.get("is_error"):
        return [(False, f"rescue review reported an error: {text}", i["message"]) for i in issues]

    verdicts = []
    for idx, issue in enumerate(issues, start=1):
        rescued, reason = claude_cli.parse_verdict(text, "RESCUE", label=f"ISSUE {idx}")
        probable_fix = claude_cli.parse_field(text, f"PROBABLE_FIX {idx}") or issue["message"]
        verdicts.append((bool(rescued), reason, probable_fix))
    return verdicts


def score_confidence(issue, file_rel_path, dependency_graph, whitelisted=True, project_adjustment=0.0):
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
    score += project_adjustment

    return round(max(0.0, min(1.0, score)), 2)


def run(project_key, severities=None, project_root=None):
    severities = severities or DEFAULT_SEVERITIES
    whitelist = load_rule_whitelist()
    dependency_graph = load_dependency_graph()
    rescue_active = LLM_RESCUE_ENABLED and project_root is not None

    project_measures = {}
    if FETCH_PROJECT_MEASURES:
        project_measures = sonar_mcp.fetch_project_measures(project_key, cwd=project_root)
        if project_measures:
            print(f"[sonar_agent] project-wide measures: {project_measures}")
    project_adjustment = project_measures_adjustment(project_measures)
    rule_description_cache = {}

    print(f"[sonar_agent] Fetching issues for '{project_key}' "
          f"(severities={severities}, {len(whitelist)} whitelisted rules, "
          f"new_code_only={NEW_CODE_ONLY}, "
          f"dependency_graph={'loaded (' + str(len(dependency_graph)) + ' files)' if dependency_graph else 'not found'}, "
          f"llm_rescue={'on' if rescue_active else ('off' if not LLM_RESCUE_ENABLED else 'off (no project_root)')}, "
          f"project_adjustment={project_adjustment:+.2f}, "
          f"fetch_rule_descriptions={FETCH_RULE_DESCRIPTIONS})")

    raw_issues = fetch_issues(project_key, severities)

    results = []
    non_whitelisted_by_file = defaultdict(list)  # file_path -> [(issue, entry), ...]

    for issue in raw_issues:
        # component is "<project_key>:<file_path>", but project_key itself may
        # contain colons (e.g. Maven's "groupId:artifactId"), so strip it by
        # known length rather than splitting on the first ":".
        component = issue["component"]
        prefix = project_key + ":"
        file_path = component[len(prefix):] if component.startswith(prefix) else component

        whitelisted = rule_matches_whitelist(issue["rule"], whitelist)

        entry = {
            "issue_key": issue["key"],
            "file": file_path,
            "line": issue.get("line"),
            "rule": issue["rule"],
            "message": issue["message"],
            "severity": issue["severity"],
            "type": issue.get("type"),
        }

        if whitelisted:
            # Already a human-vetted, pre-approved rule - the full rationale
            # a rule_description would carry isn't needed to justify or fix
            # it, so skip the (real LLM cost) fetch entirely for this path.
            entry["rule_description"] = None
            entry["needs_human_judgment"] = False
            entry["confidence"] = score_confidence(
                issue, file_path, dependency_graph, whitelisted=True, project_adjustment=project_adjustment,
            )
            entry["fan_in"] = dependency_graph.get(file_path, {}).get("fan_in")
            results.append(entry)
            continue

        entry["rule_description"] = get_rule_description(issue["rule"], rule_description_cache, project_root)
        non_whitelisted_by_file[file_path].append((issue, entry))

    # Non-whitelisted issues are handled per FILE rather than per issue: a
    # file with 10 rescue-eligible issues gets one rescue_review_batch call
    # instead of 10 separate ones - the same per-file batching fix_agent.py
    # already does for review/fix.
    for file_path, issue_entries in non_whitelisted_by_file.items():
        rescue_pairs = []
        other_pairs = []
        for issue, entry in issue_entries:
            if rescue_active and issue.get("type") == "CODE_SMELL":
                rescue_pairs.append((issue, entry))
            else:
                other_pairs.append((issue, entry))

        if rescue_pairs:
            verdicts = rescue_review_batch(project_root, file_path, [i for i, _ in rescue_pairs])
            for (issue, entry), (rescued, reason, probable_fix) in zip(rescue_pairs, verdicts):
                if rescued:
                    print(f"[sonar_agent]   rescued: {issue['rule']} in {file_path} - {reason}")
                else:
                    print(f"[sonar_agent]   not rescued: {issue['rule']} in {file_path} - {reason}")

                entry["confidence"] = score_confidence(
                    issue, file_path, dependency_graph, whitelisted=False, project_adjustment=project_adjustment,
                )
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

        for issue, entry in other_pairs:
            reason = ("rule not whitelisted, and not eligible for rescue "
                      f"(type={issue.get('type')}, rescue_enabled={LLM_RESCUE_ENABLED})")
            probable_fix = claude_cli.suggest_probable_fix(project_root, file_path, issue)

            entry["confidence"] = score_confidence(
                issue, file_path, dependency_graph, whitelisted=False, project_adjustment=project_adjustment,
            )
            entry["fan_in"] = dependency_graph.get(file_path, {}).get("fan_in")
            entry["needs_human_judgment"] = True
            entry["reason"] = reason
            entry["probable_fix"] = probable_fix

            results.append(entry)

    # Highest-confidence, easiest wins first
    results.sort(key=lambda x: x["confidence"], reverse=True)

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / "issues.json"
    with open(output_path, "w") as f:
        json.dump(
            {"generated_at": datetime.now().isoformat(timespec="seconds"), "issues": results},
            f, indent=2,
        )

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
