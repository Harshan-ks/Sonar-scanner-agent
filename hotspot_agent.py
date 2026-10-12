# hotspot_agent.py
"""
Hotspot Review Agent

Role: Lists open Security Hotspots for a project and produces a triage note
for each one - a risk assessment plus a probable fix - so a human has a
concrete starting point instead of an empty SonarQube queue entry.

This is READ-ONLY. It never calls change_security_hotspot_status or takes
any other action on SonarQube's state; a human decides what to do with each
note. That's a deliberate scope boundary, not a limitation to lift later
without discussion - hotspots are inherently judgment calls (SonarQube's own
model is "worth a human look", not "confirmed problem"), and closing them
out automatically isn't part of this system's design.

Input:   project key, the local filesystem root of the scanned project
Process: GET /api/hotspots/search (deterministic, no LLM needed just to list
         them) -> for each hotspot, one Claude call with Read plus the
         SonarQube MCP server's show_security_hotspot and show_rule tools,
         producing a risk assessment and probable fix
Output:  output/hotspot_report.json -> [{key, file, line, message,
         vulnerability_probability, security_category, status, risk,
         probable_fix}, ...]
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

import claude_cli

load_dotenv()

SONAR_URL = os.environ.get("SONAR_URL", "http://localhost:9000")
SONAR_TOKEN = os.environ["SONAR_TOKEN"]

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"

HOTSPOT_ALLOWED_TOOLS = f"Read mcp__sonarqube__show_security_hotspot {claude_cli.MCP_SHOW_RULE}"


def fetch_hotspots(project_key):
    """Calls GET /api/hotspots/search - deterministic, no LLM call needed
    just to enumerate what's open."""
    params = {"projectKey": project_key, "ps": 500, "status": "TO_REVIEW"}
    resp = requests.get(
        f"{SONAR_URL}/api/hotspots/search", params=params, auth=(SONAR_TOKEN, ""), timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("hotspots", [])


def build_triage_prompt(hotspot):
    return (
        f'Review SonarQube security hotspot "{hotspot["key"]}": {hotspot["message"]}\n\n'
        f"Use the show_security_hotspot tool (hotspotKey: {hotspot['key']}) to see "
        f"full detail - rule rationale, code context, and any existing flows or "
        f"comments - and the show_rule tool for the underlying rule's "
        f"compliant/non-compliant examples. Then read the actual file for "
        f"further context if needed.\n\n"
        "Decide: is this a real concern in this specific context, or a likely "
        "false positive? Respond with EXACTLY these two lines, nothing else:\n"
        "RISK: <one sentence - real concern, or likely false positive, and why>\n"
        "PROBABLE_FIX: <the concrete remediation a human should apply, or "
        "\"None - looks safe as written\" if not a real concern>"
    )


def triage_hotspot(project_root, hotspot):
    prompt = build_triage_prompt(hotspot)
    try:
        response = claude_cli.run_claude(prompt, cwd=project_root, allowed_tools=HOTSPOT_ALLOWED_TOOLS)
    except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as e:
        return f"(triage call failed: {e})", hotspot["message"]

    text = response.get("result")
    if response.get("is_error"):
        return f"(triage reported an error: {text})", hotspot["message"]

    risk = claude_cli.parse_field(text, "RISK") or (text or "").strip()[:300]
    probable_fix = claude_cli.parse_probable_fix(text, hotspot["message"])
    return risk, probable_fix


def run(project_key, project_root=None):
    hotspots = fetch_hotspots(project_key)
    print(f"[hotspot_agent] {len(hotspots)} open security hotspot(s) for '{project_key}' "
          f"(triage={'on' if project_root else 'off (no project_root)'})")

    results = []
    for h in hotspots:
        component = h["component"]
        prefix = project_key + ":"
        file_path = component[len(prefix):] if component.startswith(prefix) else component

        entry = {
            "key": h["key"],
            "file": file_path,
            "line": h.get("line"),
            "message": h["message"],
            "vulnerability_probability": h.get("vulnerabilityProbability"),
            "security_category": h.get("securityCategory"),
            "status": h.get("status"),
        }

        if project_root:
            risk, probable_fix = triage_hotspot(project_root, h)
            entry["risk"] = risk
            entry["probable_fix"] = probable_fix
            print(f"[hotspot_agent]   {h['key']} in {file_path}: {risk}")
        else:
            entry["risk"] = None
            entry["probable_fix"] = "(no project_root supplied - triage skipped)"

        results.append(entry)

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / "hotspot_report.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"[hotspot_agent] {len(results)} hotspot(s) written -> {output_path}")
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python hotspot_agent.py <project_key> [project_root]")
        sys.exit(1)
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
