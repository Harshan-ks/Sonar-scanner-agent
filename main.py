# main.py
"""
Orchestrator for the SonarQube pipeline. The actual orchestration logic (scan,
then conditionally fix) lives in pipeline_graph.py as a LangGraph StateGraph;
this file is just the CLI front door to it.

    python main.py serve                          Start the webhook listener (the long-running
                                                    service). On a real quality-gate failure this
                                                    always runs the Sonar Scanner Agent. The Fix
                                                    Agent only runs afterward if --auto-fix is
                                                    passed (or SONAR_AUTO_FIX=true in .env) -
                                                    otherwise it stops at issues.json for review.
    python main.py serve --auto-fix --project-root "<path>"
                                                    Same, but fully closes the loop automatically:
                                                    gate fails -> issues.json -> fixes applied.
    python main.py run <project_key> <project_root> [--auto-fix]
                                                    Run the full pipeline once, directly, without
                                                    needing a webhook call at all.
    python main.py scan <project_key> [severities] Run the Sonar Scanner Agent directly, no
                                                    webhook needed. severities is optional and
                                                    comma-separated, e.g. BLOCKER,CRITICAL,MAJOR.
    python main.py fix <project_root> [issues_file]
                                                    Run the Fix Agent directly on an existing
                                                    issues.json (defaults to output/issues.json).
"""
import argparse
import json

import fix_agent
import pipeline_graph
import sonar_agent
import webhook_listener


def cmd_serve(args):
    if args.auto_fix:
        webhook_listener.AUTO_FIX = True
    if args.project_root:
        webhook_listener.FIX_PROJECT_ROOT = args.project_root

    print(f"[main] auto_fix={webhook_listener.AUTO_FIX}  "
          f"fix_project_root={webhook_listener.FIX_PROJECT_ROOT}")
    print(f"[main] Listening on http://0.0.0.0:{webhook_listener.LISTENER_PORT}/sonar-webhook")
    webhook_listener.app.run(host="0.0.0.0", port=webhook_listener.LISTENER_PORT)


def cmd_run(args):
    result = pipeline_graph.run_pipeline(
        args.project_key, project_root=args.project_root, auto_fix=args.auto_fix,
    )
    print(json.dumps(
        {"issues": result["issues"], "fix_report": result["fix_report"]},
        indent=2, default=str,
    ))


def cmd_scan(args):
    severities = args.severities.split(",") if args.severities else None
    sonar_agent.run(args.project_key, severities)


def cmd_fix(args):
    fix_agent.run(args.project_root, args.issues_file)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Orchestrator (LangGraph): webhook listener -> Sonar Scanner Agent -> Fix Agent",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Start the webhook listener")
    p_serve.add_argument(
        "--auto-fix", action="store_true",
        help="Automatically run the Fix Agent after a gate failure (default: off)",
    )
    p_serve.add_argument(
        "--project-root",
        help="Local filesystem path to the scanned project (required for --auto-fix)",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="Run the full scan-then-fix pipeline once, no webhook needed")
    p_run.add_argument("project_key")
    p_run.add_argument("project_root")
    p_run.add_argument("--auto-fix", action="store_true", help="Also run the Fix Agent (default: off)")
    p_run.set_defaults(func=cmd_run)

    p_scan = sub.add_parser("scan", help="Run the Sonar Scanner Agent directly")
    p_scan.add_argument("project_key")
    p_scan.add_argument("severities", nargs="?", help="Comma-separated, e.g. BLOCKER,CRITICAL,MAJOR")
    p_scan.set_defaults(func=cmd_scan)

    p_fix = sub.add_parser("fix", help="Run the Fix Agent directly on an existing issues.json")
    p_fix.add_argument("project_root")
    p_fix.add_argument("issues_file", nargs="?")
    p_fix.set_defaults(func=cmd_fix)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
