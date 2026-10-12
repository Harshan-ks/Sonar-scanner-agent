# webhook_listener.py
"""
Receives SonarQube analysis webhooks. When a project's Quality Gate status is
"ERROR" (failing), it runs the pipeline_graph orchestrator (Sonar Scanner
Agent, then conditionally the Fix Agent) in a background thread within this
same process. The Fix Agent only runs if AUTO_FIX is enabled (off by
default) - otherwise the pipeline stops at issues.json so a human can review
before any code gets changed.

Also exposes /trigger, a manual control route for kicking off the same
pipeline against this already-running process instead of spawning a fresh
`main.py` invocation, and /fix, a free-text-driven route for resolving just
some of the issues already found by the last scan - see those routes below.
"""
import hashlib
import hmac
import os
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify, request

import fix_agent
import pipeline_graph
import projects_config

load_dotenv()

WEBHOOK_SECRET = os.environ.get("SONAR_WEBHOOK_SECRET")
LISTENER_PORT = int(os.environ.get("LISTENER_PORT", 5001))
AUTO_FIX = os.environ.get("SONAR_AUTO_FIX", "false").lower() == "true"

app = Flask(__name__)


def resolve_project_root(project_key):
    """Looks up project_key's local checkout in projects.yaml. Returns None
    if it isn't listed there, rather than falling back to some other
    project's root - the Fix Agent must never run against the wrong
    project's files (this was a real bug: a single global project root used
    for every incoming project_key caused the Fix Agent to run against
    Studentmanagement's checkout while trying to fix LoanPrediction's
    issues, throwing FileNotFoundError on every run). A route back to END
    without a project_root still writes issues.json; it just skips LLM
    rescue and auto-fix (see route_after_scan in pipeline_graph.py)."""
    root = projects_config.load_projects_config().get(project_key)
    if not root:
        print(f"[webhook] no local project_root for '{project_key}' in "
              f"{projects_config.PROJECTS_CONFIG_FILE.name} - scanning only, no auto-fix")
    return root


def handle_gate_failure(project_key):
    project_root = resolve_project_root(project_key)
    print(f"[webhook] Quality gate FAILED for {project_key} -> running pipeline "
          f"(auto_fix={AUTO_FIX}, project_root={project_root})")
    pipeline_graph.run_pipeline(project_key, project_root=project_root, auto_fix=AUTO_FIX)


def verify_signature(payload_body, signature_header):
    if not WEBHOOK_SECRET:
        return True  # no secret configured -> skip verification (dev mode only)
    if not signature_header:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.route("/sonar-webhook", methods=["POST"])
def sonar_webhook():
    signature = request.headers.get("X-Sonar-Webhook-HMAC-SHA256")
    if not verify_signature(request.get_data(), signature):
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    project_key = payload.get("project", {}).get("key")
    gate_status = payload.get("qualityGate", {}).get("status")

    print(f"[webhook] project={project_key} qualityGate.status={gate_status}")

    if gate_status == "ERROR":
        threading.Thread(target=handle_gate_failure, args=(project_key,), daemon=True).start()
    else:
        print(f"[webhook] Quality gate OK for {project_key} -> nothing to do")

    # Ack immediately so SonarQube's webhook call doesn't wait on the agent run
    return jsonify({"received": True}), 200


@app.route("/trigger", methods=["POST"])
def trigger():
    """Manual equivalent of a real webhook call, for hitting this already-
    running process directly instead of starting a fresh `main.py` process:

        Invoke-RestMethod -Uri http://localhost:5001/trigger -Method Post `
            -Body (@{project_key='LoanPrediction'} | ConvertTo-Json) -ContentType 'application/json'

    Runs synchronously (a human is waiting on the response here, unlike
    SonarQube's webhook sender) and returns the full result.
    """
    payload = request.get_json(force=True, silent=True) or {}
    project_key = payload.get("project_key")
    if not project_key:
        return jsonify({"error": "project_key is required"}), 400

    project_root = resolve_project_root(project_key)
    print(f"[webhook] /trigger for {project_key} -> running pipeline "
          f"(auto_fix={AUTO_FIX}, project_root={project_root})")
    result = pipeline_graph.run_pipeline(project_key, project_root=project_root, auto_fix=AUTO_FIX)
    return jsonify({"issues": result["issues"], "fix_report": result["fix_report"]}), 200


@app.route("/fix", methods=["POST"])
def fix():
    """Targeted fix on the project's existing output/issues.json (from the
    most recent scan) - no fresh scan is run. `command` is free text, e.g.
    "fix issue AZxyz123" or "resolve all critical and major issues"; it's
    resolved against the current issue list first, and only the matching
    issues are considered (see fix_agent.resolve_issue_selection):

        Invoke-RestMethod -Uri http://localhost:5001/fix -Method Post `
            -Body (@{project_key='LoanPrediction'; command='resolve all critical and major issues'} | ConvertTo-Json) `
            -ContentType 'application/json'
    """
    payload = request.get_json(force=True, silent=True) or {}
    project_key = payload.get("project_key")
    command = payload.get("command")
    if not project_key or not command:
        return jsonify({"error": "project_key and command are both required"}), 400

    project_root = resolve_project_root(project_key)
    if not project_root:
        return jsonify({"error": f"no local project_root for '{project_key}' in "
                                  f"{projects_config.PROJECTS_CONFIG_FILE.name}"}), 400

    print(f"[webhook] /fix for {project_key}: {command!r}")
    report = fix_agent.run(project_root, command=command)
    return jsonify(report), 200


if __name__ == "__main__":
    print(f"[webhook] Listening on http://0.0.0.0:{LISTENER_PORT}/sonar-webhook")
    app.run(host="0.0.0.0", port=LISTENER_PORT)
