# fix_agent.py
"""
Fix Agent

Role: Consumes output/issues.json (written by sonar_agent.py) and uses the
Claude Code CLI to directly fix every issue whose confidence score meets
FIX_CONFIDENCE_THRESHOLD. sonar_agent.py's rule whitelist is already a hard
"safe to touch" filter (see Approach_confidence_score.txt), so this
threshold is a second, tunable safety margin on top of that - not a
per-rule flag.

Before touching a file, a read-only review pass asks Claude whether the
fix is genuinely safe given the surrounding code, catching context a rigid
rule-key whitelist can't see (e.g. "this unused variable is referenced in a
docstring example"). Only if that review says SAFE does the real edit run.

Every issue that doesn't get auto-fixed - because sonar_agent.py already
flagged it needs_human_judgment, because it's below the confidence
threshold, or because the review pass rejected it - ends up in
fix_report.json's "needs_human_judgment" list with a probable_fix
suggestion, so a human always has a concrete starting point.

Input:   output/issues.json, the local filesystem root of the scanned project
Process: split by needs_human_judgment -> filter remaining by confidence ->
         group by file -> for each file: back up the original -> read-only
         review pass -> if SAFE, run `claude -p` (Read+Edit, edits
         auto-accepted) to fix it; if not, or if below threshold, record it
         with a probable fix instead
Output:  modified source files, output/backups/<run_id>/..., output/fix_report.json
"""
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import claude_cli

load_dotenv()

BASE_DIR = Path(__file__).parent
DEFAULT_ISSUES_FILE = BASE_DIR / "output" / "issues.json"
REPORT_FILE = BASE_DIR / "output" / "fix_report.json"
BACKUP_ROOT = BASE_DIR / "output" / "backups"

CONFIDENCE_THRESHOLD = float(os.environ.get("FIX_CONFIDENCE_THRESHOLD", "0.7"))
REVIEW_BEFORE_FIX = os.environ.get("FIX_REVIEW_BEFORE_FIX", "true").lower() == "true"


def load_issues(issues_file):
    with open(issues_file) as f:
        return json.load(f)


def group_by_file(issues):
    grouped = defaultdict(list)
    for issue in issues:
        grouped[issue["file"]].append(issue)
    return grouped


def backup_file(project_root, file_rel_path, run_id):
    src = project_root / file_rel_path
    dst = BACKUP_ROOT / run_id / file_rel_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def build_fix_prompt(file_rel_path, issues):
    issue_lines = "\n".join(
        f"- Line {i['line']}, rule {i['rule']} ({i['severity']}): {i['message']}"
        for i in issues
    )
    return (
        f'Fix the following SonarQube issue(s) in "{file_rel_path}":\n\n'
        f"{issue_lines}\n\n"
        "Read the file, make the smallest possible change(s) that resolve exactly "
        "these issues, and save the file. Do not refactor unrelated code or change "
        "behavior beyond what each rule requires."
    )


def build_review_prompt(file_rel_path, issues):
    issue_lines = "\n".join(
        f"- Line {i['line']}, rule {i['rule']} ({i['severity']}): {i['message']}"
        for i in issues
    )
    return (
        f'Before any fix is applied, review whether the following SonarQube '
        f'issue(s) in "{file_rel_path}" are genuinely safe to auto-fix, or '
        f"whether the surrounding code gives context the rule can't see (e.g. "
        f"a variable that looks unused but is referenced in a docstring "
        f"example, via reflection, or as a required test fixture/interface "
        f"override).\n\n"
        f"{issue_lines}\n\n"
        "Read the file and decide. Respond with EXACTLY these two lines, "
        "nothing else:\n"
        "VERDICT: SAFE - <one sentence reason>  (use UNSAFE - <reason> if it needs human judgment)\n"
        "PROBABLE_FIX: <the concrete change that resolves this - what would be applied if safe, "
        "or what a human should investigate/do if unsafe>"
    )


def review_before_fix(project_root, file_rel_path, issues):
    """Read-only sanity check (Read tool only, no Edit) run before fix_file()
    commits to an edit. Fails closed: any error, timeout, or unparseable
    response is treated as UNSAFE so a broken review never silently
    disables itself into always-approve. Always returns a probable_fix
    suggestion regardless of verdict."""
    prompt = build_review_prompt(file_rel_path, issues)
    fallback_fix = "; ".join(i["message"] for i in issues)
    try:
        response = claude_cli.run_claude(prompt, cwd=project_root, allowed_tools="Read")
    except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as e:
        return False, f"review call failed: {e}", fallback_fix

    text = response.get("result")
    if response.get("is_error"):
        return False, f"review reported an error: {text}", fallback_fix

    safe, reason = claude_cli.parse_verdict(text, "SAFE")
    probable_fix = claude_cli.parse_probable_fix(text, fallback_fix)
    if safe is None:
        return False, reason, probable_fix
    return safe, reason, probable_fix


def apply_fix(project_root, file_rel_path, issues):
    prompt = build_fix_prompt(file_rel_path, issues)
    return claude_cli.run_claude(prompt, cwd=project_root, allowed_tools="Read Edit")


def fix_file(project_root, file_rel_path, issues, run_id):
    """Returns (fixed_records, human_judgment_records)."""
    file_path = project_root / file_rel_path
    original_code = file_path.read_text(encoding="utf-8")

    if REVIEW_BEFORE_FIX:
        safe, reason, probable_fix = review_before_fix(project_root, file_rel_path, issues)
        if not safe:
            print(f"[fix_agent]   SKIPPED {file_rel_path}: review flagged unsafe - {reason}")
            human = [{**i, "reason": reason, "probable_fix": probable_fix} for i in issues]
            return [], human
        print(f"[fix_agent]   review passed for {file_rel_path}: {reason}")

    backup_file(project_root, file_rel_path, run_id)

    try:
        response = apply_fix(project_root, file_rel_path, issues)
    except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as e:
        print(f"[fix_agent]   FAILED on {file_rel_path}: {e}")
        return [{**i, "explanation": None, "fix_status": "failed", "error": str(e)} for i in issues], []

    new_code = file_path.read_text(encoding="utf-8")
    changed = new_code != original_code

    if response.get("is_error"):
        status = "failed"
        print(f"[fix_agent]   claude reported an error on {file_rel_path}: {response.get('result')}")
    elif not changed:
        status = "no_change"
        print(f"[fix_agent]   no changes produced for {file_rel_path}")
    else:
        status = "fixed"
        print(f"[fix_agent]   wrote {file_rel_path} ({len(issues)} issue(s) addressed, "
              f"${response.get('total_cost_usd', 0):.4f})")

    explanation = response.get("result")
    return [{**i, "explanation": explanation, "fix_status": status} for i in issues], []


def run(project_root, issues_file=None):
    project_root = Path(project_root)
    issues_file = Path(issues_file) if issues_file else DEFAULT_ISSUES_FILE
    issues = load_issues(issues_file)

    already_human = [i for i in issues if i.get("needs_human_judgment")]
    candidates = [i for i in issues if not i.get("needs_human_judgment")]

    fixable = [i for i in candidates if i.get("confidence", 0) >= CONFIDENCE_THRESHOLD]
    below_threshold = [i for i in candidates if i.get("confidence", 0) < CONFIDENCE_THRESHOLD]

    print(f"[fix_agent] {len(fixable)} issue(s) >= confidence threshold {CONFIDENCE_THRESHOLD}, "
          f"{len(below_threshold)} below threshold, {len(already_human)} already flagged by "
          f"sonar_agent, review_before_fix={REVIEW_BEFORE_FIX}")

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = {
        "run_id": run_id,
        "project_root": str(project_root),
        "fixed": [],
        "needs_human_judgment": list(already_human),
    }

    for issue in below_threshold:
        probable_fix = claude_cli.suggest_probable_fix(project_root, issue["file"], issue)
        report["needs_human_judgment"].append({
            **issue,
            "reason": f"confidence {issue.get('confidence', 0)} is below threshold {CONFIDENCE_THRESHOLD}",
            "probable_fix": probable_fix,
        })

    for file_rel_path, file_issues in group_by_file(fixable).items():
        print(f"[fix_agent] Considering {len(file_issues)} issue(s) in {file_rel_path}")
        fixed, human = fix_file(project_root, file_rel_path, file_issues, run_id)
        report["fixed"].extend(fixed)
        report["needs_human_judgment"].extend(human)

    REPORT_FILE.parent.mkdir(exist_ok=True)
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    fixed_count = sum(1 for r in report["fixed"] if r["fix_status"] == "fixed")
    print(f"[fix_agent] Done. {fixed_count}/{len(fixable)} issue(s) fixed, "
          f"{len(report['needs_human_judgment'])} left for human judgment. "
          f"Backups in {BACKUP_ROOT / run_id} -> report at {REPORT_FILE}")
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fix_agent.py <project_root_path> [issues_file]")
        sys.exit(1)
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
