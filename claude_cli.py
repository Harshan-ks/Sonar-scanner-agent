# claude_cli.py
"""
Thin wrapper around the Claude Code CLI for headless, tool-scoped calls.

Used by fix_agent.py (to review and apply fixes) and sonar_agent.py (to
rescue-review issues whose rule isn't on the whitelist, and to suggest
probable fixes for anything left for human judgment). This shells out to
`claude -p` rather than calling the Anthropic API directly (e.g. via
langchain_anthropic): the CLAUDE_CODE_OAUTH_TOKEN hits persistent 429s on
direct /v1/messages calls, but the CLI's own request path doesn't.
"""
import json
import re
import subprocess

CLAUDE_BIN = "claude"
DEFAULT_TIMEOUT_SECONDS = 300


def run_claude(prompt, cwd, allowed_tools="Read", permission_mode="acceptEdits",
                timeout=DEFAULT_TIMEOUT_SECONDS):
    """Runs one headless Claude Code turn scoped to `allowed_tools`, returns
    the parsed --output-format json response dict. Raises on non-zero exit,
    timeout, or unparseable output - callers decide how to fail (open/closed)."""
    result = subprocess.run(
        [
            CLAUDE_BIN, "-p", prompt,
            "--permission-mode", permission_mode,
            "--allowedTools", allowed_tools,
            "--output-format", "json",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr[:2000]}")
    return json.loads(result.stdout)


def parse_field(response_text, label):
    """Extracts a "<LABEL>: <value>" line (case-insensitive) from a
    response. Only matches within a single line, since prompts ask for
    exactly one line per field."""
    pattern = re.compile(rf"^{re.escape(label)}:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(response_text or "")
    return match.group(1).strip() if match else None


def parse_verdict(response_text, true_word, label="VERDICT"):
    """Parses a "<LABEL>: <WORD> - <reason>" line into
    (matches_true_word: bool | None, reason: str). None means the line
    couldn't be found at all - callers should treat that as a failure
    (fail closed), not as a default answer either way."""
    value = parse_field(response_text, label)
    if value is None:
        return None, f"could not parse a verdict from: {(response_text or '(empty)')[:300]}"
    word, _, reason = value.partition("-")
    return word.strip().upper() == true_word.upper(), (reason.strip() or value)


def parse_probable_fix(response_text, fallback):
    return parse_field(response_text, "PROBABLE_FIX") or fallback


def build_probable_fix_prompt(file_rel_path, issue):
    return (
        f'A SonarQube issue in "{file_rel_path}" is being left for human review '
        f"rather than an automated fix:\n\n"
        f"- Line {issue.get('line')}, rule {issue['rule']} "
        f"({issue['severity']}, type {issue.get('type')}): {issue['message']}\n\n"
        "Read the file for context, then suggest a concrete, specific fix a "
        "human could apply. Name the actual change - don't be generic.\n\n"
        "Respond with EXACTLY one line in this format, nothing else:\n"
        "PROBABLE_FIX: <your suggestion, one to two sentences>"
    )


def suggest_probable_fix(project_root, file_rel_path, issue):
    """Read-only Claude Code call suggesting a fix for an issue that's being
    left for human judgment without ever going through a review/rescue call
    that would already carry its own probable_fix. Falls back to Sonar's own
    message if no project_root is available, or on any call failure."""
    if not project_root:
        return issue["message"]
    prompt = build_probable_fix_prompt(file_rel_path, issue)
    try:
        response = run_claude(prompt, cwd=project_root, allowed_tools="Read")
    except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError):
        return issue["message"]
    if response.get("is_error"):
        return issue["message"]
    return parse_probable_fix(response.get("result"), issue["message"])
