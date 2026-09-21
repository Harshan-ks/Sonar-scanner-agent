# webhook_listener.py
"""
Receives SonarQube analysis webhooks. When a project's Quality Gate status is
"ERROR" (failing), it runs the pipeline_graph orchestrator (Sonar Scanner
Agent, then conditionally the Fix Agent) in a background thread within this
same process. The Fix Agent only runs if AUTO_FIX is enabled (off by
default) - otherwise the pipeline stops at issues.json so a human can review
before any code gets changed.
"""
import hashlib
import hmac
import os
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify, request

import pipeline_graph

load_dotenv()

WEBHOOK_SECRET = os.environ.get("SONAR_WEBHOOK_SECRET")
LISTENER_PORT = int(os.environ.get("LISTENER_PORT", 5001))
AUTO_FIX = os.environ.get("SONAR_AUTO_FIX", "false").lower() == "true"
FIX_PROJECT_ROOT = os.environ.get("FIX_PROJECT_ROOT")

app = Flask(__name__)


def handle_gate_failure(project_key):
    print(f"[webhook] Quality gate FAILED for {project_key} -> running pipeline "
          f"(auto_fix={AUTO_FIX})")
    pipeline_graph.run_pipeline(project_key, project_root=FIX_PROJECT_ROOT, auto_fix=AUTO_FIX)


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


if __name__ == "__main__":
    print(f"[webhook] Listening on http://0.0.0.0:{LISTENER_PORT}/sonar-webhook")
    app.run(host="0.0.0.0", port=LISTENER_PORT)
