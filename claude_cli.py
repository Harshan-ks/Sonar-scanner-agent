# claude_cli.py
"""
Thin wrapper around the Claude Code CLI for headless, tool-scoped calls.

Used by fix_agent.py (to review and apply fixes) and sonar_agent.py (to
rescue-review issues whose rule isn't on the whitelist). This shells out to
`claude -p` rather than calling the Anthropic API directly (e.g. via
langchain_anthropic): the CLAUDE_CODE_OAUTH_TOKEN hits persistent 429s on
direct /v1/messages calls, but the CLI's own request path doesn't.
"""
import json
import re
import subprocess

CLAUDE_BIN = "claude"
DEFAULT_TIMEOUT_SECONDS = 300

VERDICT_RE = re.compile(r"VERDICT:\s*(\w+)\s*-\s*(.+)", re.IGNORECASE | re.DOTALL)


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


def parse_verdict(response_text, true_word):
    """Parses a one-line "VERDICT: <WORD> - <reason>" response into
    (matches_true_word: bool | None, reason: str). None means the response
    couldn't be parsed at all - callers should treat that as a failure
    (fail closed), not as a default answer either way."""
    match = VERDICT_RE.search(response_text or "")
    if not match:
        return None, f"could not parse a verdict from: {(response_text or '(empty)')[:300]}"
    verdict, reason = match.group(1).upper(), match.group(2).strip()
    return verdict == true_word.upper(), reason
