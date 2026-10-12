# main.py
"""
Orchestrator for the SonarQube pipeline. The actual orchestration logic (scan,
then conditionally fix) lives in pipeline_graph.py as a LangGraph StateGraph;
this file is just the CLI front door to it.

    python main.py serve                          Start the webhook listener (the long-running
                                                    service). On a real quality-gate failure this
                                                    always runs the Sonar Scanner Agent, using the
                                                    project_root looked up per project_key from
                                                    projects.yaml. The Fix Agent only runs afterward
                                                    if --auto-fix is passed (or SONAR_AUTO_FIX=true
                                                    in .env) - otherwise it stops at issues.json for
                                                    review. Also exposes POST /trigger {project_key}
                                                    to manually kick off the same pipeline against
                                                    this running process instead of a fresh process.
    python main.py serve --auto-fix                Same, but fully closes the loop automatically:
                                                    gate fails -> issues.json -> fixes applied.
    python main.py run <project_key> <project_root> [--auto-fix]
                                                    Run the full pipeline once, directly, without
                                                    needing a webhook call at all.
    python main.py scan <project_key> [severities] [project_root]
                                                    Run the Sonar Scanner Agent directly, no
                                                    webhook needed. severities is optional and
                                                    comma-separated, e.g. BLOCKER,CRITICAL,MAJOR.
                                                    project_root enables LLM rescue if
                                                    SONAR_LLM_RESCUE=true.
    python main.py fix <project_root> [issues_file] [--command "<free text>"]
                                                    Run the Fix Agent directly on an existing
                                                    issues.json (defaults to output/issues.json).
                                                    Without --command, every fixable issue is
                                                    considered. With --command, e.g. "fix issue
                                                    AZxyz123" or "resolve all critical and major
                                                    issues", only the issues that request resolves
                                                    to are considered - same confidence threshold
                                                    and review-before-fix gate either way.
    python main.py hotspots <project_key> [project_root]
                                                    Run the Hotspot Review Agent: lists open
                                                    Security Hotspots and, if project_root is
                                                    given, triages each with a probable fix.
                                                    Read-only - never changes hotspot status.
    python main.py projects                        List every SonarQube project the MCP server
                                                    can see (routed through Claude + MCP - a real
                                                    LLM call, not a free REST fetch). Use this to
                                                    help populate projects.yaml.
    python main.py measures <project_key>           Print project-wide health metrics (coverage,
                                                    complexity, duplication) via MCP.
    python main.py metrics                          List available SonarQube metric keys via MCP
                                                    (one-time reference, not for repeated use).
    python main.py scan-all [--auto-fix]            Discover every project via MCP, cross-reference
                                                    projects.yaml for local roots, and run the full
                                                    pipeline on each one that has a known root.
"""
import argparse
import json

import fix_agent
import hotspot_agent
import pipeline_graph
import projects_config
import sonar_agent
import sonar_mcp
import webhook_listener


def cmd_serve(args):
    if args.auto_fix:
        webhook_listener.AUTO_FIX = True

    print(f"[main] auto_fix={webhook_listener.AUTO_FIX}  "
          f"(project roots are resolved per-project from {projects_config.PROJECTS_CONFIG_FILE.name})")
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
    sonar_agent.run(args.project_key, severities, project_root=args.project_root)



def cmd_fix(args):
    fix_agent.run(args.project_root, args.issues_file, command=args.command)


def cmd_hotspots(args):
    hotspot_agent.run(args.project_key, args.project_root)


def cmd_projects(args):
    for p in sonar_mcp.discover_projects():
        print(f"{p['key']}  |  {p['name']}")


def cmd_measures(args):
    measures = sonar_mcp.fetch_project_measures(args.project_key)
    if not measures:
        print(f"[main] no measures returned for '{args.project_key}'")
        return
    for k, v in measures.items():
        print(f"{k} = {v}")


def cmd_metrics(args):
    for m in sonar_mcp.list_available_metrics():
        print(f"{m['key']}  |  {m['name']}")


def cmd_scan_all(args):
    discovered = sonar_mcp.discover_projects()
    local_roots = projects_config.load_projects_config()
    print(f"[main] discovered {len(discovered)} project(s), "
          f"{len(local_roots)} with a known local root in {projects_config.PROJECTS_CONFIG_FILE.name}")

    for p in discovered:
        key = p["key"]
        root = local_roots.get(key)
        if not root:
            print(f"[main] skipping '{key}' ({p['name']}) - no local project_root in {projects_config.PROJECTS_CONFIG_FILE.name}")
            continue
        print(f"[main] scanning '{key}' at {root}")
        result = pipeline_graph.run_pipeline(key, project_root=root, auto_fix=args.auto_fix)
        fixed_count = sum(1 for r in (result["fix_report"] or {}).get("fixed", []) if r.get("fix_status") == "fixed")
        print(f"[main]   '{key}': {len(result['issues'])} issue(s) found, {fixed_count} fixed")


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
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="Run the full scan-then-fix pipeline once, no webhook needed")
    p_run.add_argument("project_key")
    p_run.add_argument("project_root")
    p_run.add_argument("--auto-fix", action="store_true", help="Also run the Fix Agent (default: off)")
    p_run.set_defaults(func=cmd_run)

    p_scan = sub.add_parser("scan", help="Run the Sonar Scanner Agent directly")
    p_scan.add_argument("project_key")
    p_scan.add_argument("severities", nargs="?", help="Comma-separated, e.g. BLOCKER,CRITICAL,MAJOR")
    p_scan.add_argument("project_root", nargs="?", help="Enables LLM rescue if SONAR_LLM_RESCUE=true")
    p_scan.set_defaults(func=cmd_scan)

    p_fix = sub.add_parser("fix", help="Run the Fix Agent directly on an existing issues.json")
    p_fix.add_argument("project_root")
    p_fix.add_argument("issues_file", nargs="?")
    p_fix.add_argument(
        "--command",
        help='Free-text request, e.g. "fix issue AZxyz123" or "resolve all critical '
             'and major issues" - only matching issues are considered (default: all)',
    )
    p_fix.set_defaults(func=cmd_fix)

    p_hotspots = sub.add_parser("hotspots", help="Run the Hotspot Review Agent (read-only)")
    p_hotspots.add_argument("project_key")
    p_hotspots.add_argument("project_root", nargs="?", help="Enables triage; omit to just list hotspots")
    p_hotspots.set_defaults(func=cmd_hotspots)

    p_projects = sub.add_parser("projects", help="List SonarQube projects via MCP (real LLM call)")
    p_projects.set_defaults(func=cmd_projects)

    p_measures = sub.add_parser("measures", help="Print project-wide health metrics via MCP")
    p_measures.add_argument("project_key")
    p_measures.set_defaults(func=cmd_measures)

    p_metrics = sub.add_parser("metrics", help="List available SonarQube metric keys via MCP")
    p_metrics.set_defaults(func=cmd_metrics)

    p_scan_all = sub.add_parser("scan-all", help="Run the pipeline across every project in projects.yaml")
    p_scan_all.add_argument("--auto-fix", action="store_true", help="Also run the Fix Agent (default: off)")
    p_scan_all.set_defaults(func=cmd_scan_all)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
