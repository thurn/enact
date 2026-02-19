"""Formatting helpers for Claude Code transcript summaries.

Provides functions to format tool inputs and results
from Claude Code session transcripts into human-readable
markdown.
"""

import re


def strip_xml_tags(text: str) -> str:
    """Remove XML-style tags from transcript content."""
    text = re.sub(
        r"<system-reminder>.*?</system-reminder>",
        "", text, flags=re.DOTALL,
    )
    text = re.sub(
        r"<local-command-caveat>.*?</local-command-caveat>",
        "", text, flags=re.DOTALL,
    )
    text = re.sub(
        r"<local-command-stdout>.*?</local-command-stdout>",
        "", text, flags=re.DOTALL,
    )
    return text.strip()


SKIP_COMMANDS = {
    "clear", "compact", "help", "status", "cost",
}


def extract_user_prompt(content) -> str | None:
    """Extract the human-readable user prompt.

    Returns None for non-substantive slash commands
    like /clear.
    """
    if isinstance(content, str):
        m = re.search(
            r"<command-name>(.*?)</command-name>",
            content,
        )
        if m:
            cmd = m.group(1).lstrip("/")
            if cmd in SKIP_COMMANDS:
                return None
            args_m = re.search(
                r"<command-args>(.*?)</command-args>",
                content,
            )
            args = args_m.group(1) if args_m else ""
            return f"/{cmd} {args}".strip()
        cleaned = strip_xml_tags(content)
        if cleaned:
            return cleaned
    return None


def _oneline(text: str) -> str:
    """Collapse text to a single line for inline display."""
    return re.sub(r"\s+", " ", text).strip()


def _format_read(inp: dict) -> str:
    path = inp.get("file_path", "?")
    parts = [f"**{path}**"]
    if "offset" in inp:
        parts.append(f"from line {inp['offset']}")
    if "limit" in inp:
        parts.append(f"({inp['limit']} lines)")
    return " ".join(parts)


def _format_edit(inp: dict) -> str:
    path = inp.get("file_path", "?")
    old = inp.get("old_string", "")
    new = inp.get("new_string", "")
    replace_all = inp.get("replace_all", False)
    result = f"**{path}**"
    if replace_all:
        result += " (replace all)"
    result += "\n"
    if old:
        old_preview = old[:300]
        if len(old) > 300:
            old_preview += "..."
        result += (
            f"  - Remove: `{_oneline(old_preview)}`\n"
        )
    if new:
        new_preview = new[:300]
        if len(new) > 300:
            new_preview += "..."
        result += (
            f"  - Insert: `{_oneline(new_preview)}`"
        )
    return result


def _format_write(inp: dict) -> str:
    path = inp.get("file_path", "?")
    content = inp.get("content", "")
    return f"**{path}** ({len(content)} chars)"


def _format_bash(inp: dict) -> str:
    cmd = inp.get("command", "?")
    desc = inp.get("description", "")
    result = f"`{cmd}`"
    if desc:
        result += f" — {desc}"
    return result


def _format_glob(inp: dict) -> str:
    pattern = inp.get("pattern", "?")
    path = inp.get("path", "")
    if path:
        return f"`{pattern}` in {path}"
    return f"`{pattern}`"


def _format_grep(inp: dict) -> str:
    pattern = inp.get("pattern", "?")
    path = inp.get("path", "")
    mode = inp.get("output_mode", "files_with_matches")
    result = f"`{pattern}`"
    if path:
        result += f" in {path}"
    if mode != "files_with_matches":
        result += f" (mode: {mode})"
    return result


def _format_task(inp: dict) -> str:
    desc = inp.get("description", "")
    agent_type = inp.get("subagent_type", "")
    prompt = inp.get("prompt", "")
    team = inp.get("team_name", "")
    agent_name = inp.get("name", "")
    result = ""
    if desc:
        result += f"**{desc}**"
    if agent_type:
        result += f" (type: {agent_type})"
    if team:
        result += f" [team: {team}]"
    if agent_name:
        result += f" as {agent_name}"
    if prompt:
        prompt_preview = prompt[:200]
        if len(prompt) > 200:
            prompt_preview += "..."
        result += f"\n  Prompt: {prompt_preview}"
    return result


def _format_send_message(inp: dict) -> str:
    msg_type = inp.get("type", "message")
    recipient = inp.get("recipient", "")
    content = inp.get("content", "")
    summary = inp.get("summary", "")
    if msg_type == "shutdown_request":
        return f"shutdown request -> {recipient}"
    if msg_type == "shutdown_response":
        approved = inp.get("approve", False)
        status = "approved" if approved else "rejected"
        return f"shutdown {status}"
    if msg_type == "broadcast":
        preview = (summary or content)[:100]
        return f"broadcast: {preview}"
    if msg_type == "plan_approval_response":
        approved = inp.get("approve", False)
        status = "approved" if approved else "rejected"
        return f"plan {status} -> {recipient}"
    preview = summary or content[:100]
    return f"-> {recipient}: {preview}"


def _format_team_create(inp: dict) -> str:
    team = inp.get("team_name", "?")
    desc = inp.get("description", "")
    if desc:
        return f"**{team}** — {desc}"
    return f"**{team}**"


def _format_mcp(name: str, inp: dict) -> str:
    short_name = name.split("__")[-1]
    info_keys = (
        "url", "phabricator_diff_number",
        "pattern", "keywords",
        "natural_language_query",
        "comment", "diff_num",
    )
    for key in info_keys:
        if key in inp:
            val = str(inp[key])
            if len(val) > 150:
                val = val[:150] + "..."
            return f"{short_name}: {val}"
    return short_name


def _format_generic(inp: dict) -> str:
    keys = list(inp.keys())
    if len(keys) <= 3:
        parts = []
        for k in keys:
            v = str(inp[k])
            if len(v) > 80:
                v = v[:80] + "..."
            parts.append(f"{k}={v}")
        return ", ".join(parts)
    return f"({len(keys)} parameters)"


_TOOL_FORMATTERS = {
    "Read": _format_read,
    "Edit": _format_edit,
    "Write": _format_write,
    "Bash": _format_bash,
    "Glob": _format_glob,
    "Grep": _format_grep,
    "Task": _format_task,
    "SendMessage": _format_send_message,
    "TeamCreate": _format_team_create,
}


def format_tool_input(name: str, inp: dict) -> str:
    """Format tool input parameters in a readable way."""
    formatter = _TOOL_FORMATTERS.get(name)
    if formatter:
        return formatter(inp)

    if name == "WebSearch":
        return f'"{inp.get("query", "?")}"'
    if name == "WebFetch":
        return inp.get("url", "?")
    if name == "TeamDelete":
        return "(cleanup)"
    if name == "AskUserQuestion":
        questions = inp.get("questions", [])
        parts = [q.get("question", "?") for q in questions]
        return "; ".join(parts)
    if name == "TaskCreate":
        return f'"{inp.get("subject", "?")}"'
    if name == "TaskUpdate":
        tid = inp.get("taskId", "?")
        status = inp.get("status", "")
        if status:
            return f"task {tid} -> {status}"
        return f"task {tid}"
    if name == "Skill":
        return inp.get("skill", "?")
    if name.startswith("mcp__"):
        return _format_mcp(name, inp)
    return _format_generic(inp)


def format_tool_result(entry: dict) -> str:
    """Format a tool result in a readable way."""
    tur = entry.get("toolUseResult")
    msg_content = (
        entry.get("message", {}).get("content", [])
    )

    result_text = ""
    is_error = False

    if isinstance(msg_content, list):
        for c in msg_content:
            if not isinstance(c, dict):
                continue
            if c.get("type") != "tool_result":
                continue
            is_error = c.get("is_error", False)
            rc = c.get("content", "")
            if isinstance(rc, str):
                result_text = rc
            elif isinstance(rc, list):
                for part in rc:
                    if isinstance(part, dict):
                        result_text += part.get(
                            "text", "",
                        )

    result_text = strip_xml_tags(result_text)

    if isinstance(tur, dict):
        formatted = _format_structured_result(tur)
        if formatted is not None:
            return formatted

    if is_error:
        preview = result_text[:300]
        if len(result_text) > 300:
            preview += "..."
        return f"ERROR: {preview}"

    if result_text:
        preview = result_text[:300]
        if len(result_text) > 300:
            preview += "..."
        return preview

    return "(no result)"


def _format_structured_result(tur: dict) -> str | None:
    """Format a structured toolUseResult dict.

    Returns None if the structure is not recognized.
    """
    tur_type = tur.get("type", "")

    if tur_type == "text" and "file" in tur:
        f = tur["file"]
        path = f.get("filePath", "?")
        num_lines = f.get("numLines", 0)
        total = f.get("totalLines", 0)
        return (
            f"Read {num_lines} lines "
            f"from {path} ({total} total)"
        )

    if "structuredPatch" in tur:
        path = tur.get("filePath", "?")
        return f"Edited {path}"

    if "stdout" in tur:
        return _format_bash_result(tur)

    if "filenames" in tur:
        filenames = tur["filenames"]
        n = tur.get("numFiles", len(filenames))
        if n <= 5:
            return "Found: " + ", ".join(filenames)
        return f"Found {n} files"

    if "agentId" in tur:
        return _format_agent_result(tur)

    return None


def _format_bash_result(tur: dict) -> str:
    stdout = tur.get("stdout", "")
    stderr = tur.get("stderr", "")
    result = ""
    if stdout:
        preview = stdout.strip()[:300]
        if len(stdout.strip()) > 300:
            preview += "..."
        result += f"stdout: {preview}"
    if stderr:
        preview = stderr.strip()[:200]
        if len(stderr.strip()) > 200:
            preview += "..."
        if result:
            result += "\n"
        result += f"stderr: {preview}"
    if not result:
        result = "(no output)"
    return result


def _format_agent_result(tur: dict) -> str:
    agent_id = tur["agentId"]
    status = tur.get("status", "?")
    tokens = tur.get("totalTokens", 0)
    tools = tur.get("totalToolUseCount", 0)
    result_content = ""
    for c in tur.get("content", []):
        if isinstance(c, dict) and c.get("type") == "text":
            result_content += c.get("text", "")
    preview = result_content[:300]
    if len(result_content) > 300:
        preview += "..."
    line = f"Agent {agent_id} ({status})"
    if tokens:
        line += (
            f" — {tokens:,} tokens, {tools} tool calls"
        )
    if preview:
        line += f"\n  Result: {preview}"
    return line
