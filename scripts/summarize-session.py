#!/usr/bin/env python3
"""Summarize a Claude Code session transcript.

Given a session ID or path to a .jsonl transcript, parses
the transcript and prints a detailed markdown summary to
STDOUT including: the initial prompt, thinking process,
tool calls and their results, files modified, and the
final output.

Usage:
    summarize-session.py <session_id>
    summarize-session.py <path/to/agent.jsonl>
    summarize-session.py --latest
"""

import argparse
import json
import sys
from pathlib import Path

from summarize_formatters import (
    extract_user_prompt,
    format_tool_input,
    format_tool_result,
)


def find_project_dirs() -> list[Path]:
    """Find all project dirs under ~/.claude/projects/."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []
    return [
        d for d in projects_dir.iterdir() if d.is_dir()
    ]


def find_transcript_by_session_id(
    session_id: str,
) -> Path | None:
    """Find a transcript .jsonl file by session UUID."""
    for project_dir in find_project_dirs():
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def find_transcript_by_agent_id(
    agent_id: str,
) -> Path | None:
    """Find a subagent transcript by agent ID."""
    agent_id = agent_id.removeprefix("agent-")
    for project_dir in find_project_dirs():
        for session_dir in project_dir.iterdir():
            if not session_dir.is_dir():
                continue
            subagents = session_dir / "subagents"
            if not subagents.is_dir():
                continue
            candidate = (
                subagents / f"agent-{agent_id}.jsonl"
            )
            if candidate.is_file():
                return candidate
    return None


def find_transcript_by_team_ref(
    team_ref: str,
) -> Path | None:
    """Find a team member transcript by reference.

    Accepts references like
    'review-foundation/reviewer-1' or
    '1771028742-review-foundation/reviewer-1'.
    """
    if "/" not in team_ref:
        return None

    team_part, agent_part = team_ref.rsplit("/", 1)

    for project_dir in find_project_dirs():
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file) as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        team = d.get("teamName", "")
                        agent = d.get("agentName", "")
                        if not team or not agent:
                            continue
                        if agent != agent_part:
                            break
                        if (
                            team == team_part
                            or team.endswith(
                                f"-{team_part}"
                            )
                        ):
                            return jsonl_file
                        break
            except (OSError, UnicodeDecodeError):
                continue
    return None


def find_latest_transcript() -> Path | None:
    """Find the most recently modified transcript."""
    latest = None
    latest_mtime = 0
    for project_dir in find_project_dirs():
        for jsonl_file in project_dir.glob("*.jsonl"):
            mtime = jsonl_file.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = jsonl_file
    return latest


def resolve_transcript(identifier: str) -> Path | None:
    """Resolve an identifier to a transcript path.

    Tries in order:
    1. Direct file path
    2. Session UUID
    3. Agent ID (with or without 'agent-' prefix)
    4. Team reference (team-name/agent-name)
    """
    p = Path(identifier)
    if p.is_file():
        return p

    result = find_transcript_by_session_id(identifier)
    if result:
        return result

    result = find_transcript_by_agent_id(identifier)
    if result:
        return result

    result = find_transcript_by_team_ref(identifier)
    if result:
        return result

    return None


def parse_transcript(path: Path) -> list[dict]:
    """Parse a .jsonl transcript into JSON objects."""
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def deduplicate_assistant_messages(
    entries: list[dict],
) -> list[dict]:
    """Deduplicate streamed assistant messages.

    Keeps only the last occurrence for each message ID.
    Returns entries in original order with earlier
    duplicates removed.
    """
    last_index: dict[str, int] = {}
    for i, entry in enumerate(entries):
        if (
            entry.get("type") == "assistant"
            and "message" in entry
        ):
            msg_id = entry["message"].get("id", "")
            if msg_id:
                last_index[msg_id] = i

    last_indices = set(last_index.values())

    result = []
    for i, entry in enumerate(entries):
        if (
            entry.get("type") == "assistant"
            and "message" in entry
        ):
            msg_id = entry["message"].get("id", "")
            if msg_id and i not in last_indices:
                continue
        result.append(entry)
    return result


def _extract_metadata(entries: list[dict]) -> dict:
    """Extract session metadata from transcript."""
    meta = {
        "session_id": "",
        "cwd": "",
        "version": "",
        "model": "",
        "timestamp_start": "",
        "timestamp_end": "",
        "team_name": "",
        "agent_name": "",
    }
    for entry in entries:
        if "sessionId" in entry and not meta["session_id"]:
            meta["session_id"] = entry["sessionId"]
        if "cwd" in entry and not meta["cwd"]:
            meta["cwd"] = entry["cwd"]
        if "version" in entry and not meta["version"]:
            meta["version"] = entry["version"]
        if "teamName" in entry and not meta["team_name"]:
            meta["team_name"] = entry["teamName"]
        if (
            "agentName" in entry
            and not meta["agent_name"]
        ):
            meta["agent_name"] = entry["agentName"]
        if "timestamp" in entry:
            if not meta["timestamp_start"]:
                meta["timestamp_start"] = (
                    entry["timestamp"]
                )
            meta["timestamp_end"] = entry["timestamp"]
        if (
            entry.get("type") == "assistant"
            and not meta["model"]
        ):
            m = (
                entry.get("message", {}).get("model", "")
            )
            if m:
                meta["model"] = m
    return meta


def _print_header(path: Path, meta: dict) -> None:
    """Print the markdown header for a transcript."""
    print("# Session Summary")
    print()
    if meta["session_id"]:
        print(f"- **Session**: `{meta['session_id']}`")
    if path.name.startswith("agent-"):
        agent_id = path.stem.removeprefix("agent-")
        print(f"- **Agent ID**: `{agent_id}`")
    if meta["team_name"]:
        print(f"- **Team**: {meta['team_name']}")
    if meta["agent_name"]:
        print(f"- **Agent Name**: {meta['agent_name']}")
    if meta["model"]:
        print(f"- **Model**: {meta['model']}")
    if meta["cwd"]:
        print(f"- **Working Directory**: `{meta['cwd']}`")
    if meta["version"]:
        print(
            "- **Claude Code Version**: "
            f"{meta['version']}"
        )
    if meta["timestamp_start"]:
        print(f"- **Started**: {meta['timestamp_start']}")
    if (
        meta["timestamp_end"]
        and meta["timestamp_end"] != meta["timestamp_start"]
    ):
        print(f"- **Ended**: {meta['timestamp_end']}")
    print(f"- **Transcript**: `{path}`")
    print()


def _process_user_entry(
    entry: dict,
    turn_number: int,
    tool_call_map: dict,
) -> int:
    """Process a user entry, return updated turn_number."""
    msg = entry.get("message", {})
    content = msg.get("content", "")

    prompt = extract_user_prompt(content)
    if prompt and not isinstance(content, list):
        turn_number += 1
        print(f"## Turn {turn_number}: User Prompt")
        print()
        if len(prompt) > 500:
            print(prompt[:500] + "...")
        else:
            print(prompt)
        print()

    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") != "tool_result":
                continue
            tool_use_id = c.get("tool_use_id", "")
            if tool_call_map.get(tool_use_id):
                result_text = format_tool_result(entry)
                print(f"  - **Result**: {result_text}")
                print()

    return turn_number


def _process_assistant_entry(
    entry: dict,
    tool_call_map: dict,
    files_modified: set,
    tools_used: dict,
) -> None:
    """Process an assistant entry."""
    msg = entry.get("message", {})
    content = msg.get("content", [])

    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")

        if block_type == "thinking":
            thinking = block.get("thinking", "")
            if thinking:
                print("### Thinking")
                print()
                if len(thinking) > 1000:
                    print(thinking[:1000])
                    remaining = len(thinking) - 1000
                    print(
                        f"\n... ({remaining}"
                        " more characters)"
                    )
                else:
                    print(thinking)
                print()

        elif block_type == "text":
            text = block.get("text", "").strip()
            if text:
                print("### Assistant Response")
                print()
                print(text)
                print()

        elif block_type == "tool_use":
            tool_name = block.get("name", "?")
            tool_id = block.get("id", "")
            inp = block.get("input", {})

            tools_used[tool_name] = (
                tools_used.get(tool_name, 0) + 1
            )
            tool_call_map[tool_id] = {
                "name": tool_name,
                "input": inp,
            }

            if tool_name in ("Edit", "Write"):
                fp = inp.get("file_path", "")
                if fp:
                    files_modified.add(fp)

            formatted = format_tool_input(
                tool_name, inp,
            )
            print(f"- **{tool_name}**: {formatted}")


def summarize_transcript(path: Path) -> None:
    """Parse and print a summary of a transcript."""
    entries = parse_transcript(path)
    if not entries:
        print("(empty transcript)")
        return

    entries = deduplicate_assistant_messages(entries)
    meta = _extract_metadata(entries)
    _print_header(path, meta)

    turn_number = 0
    tool_call_map: dict[str, dict] = {}
    files_modified: set[str] = set()
    tools_used: dict[str, int] = {}

    for entry in entries:
        entry_type = entry.get("type")
        if entry_type == "user":
            turn_number = _process_user_entry(
                entry, turn_number, tool_call_map,
            )
        elif entry_type == "assistant":
            _process_assistant_entry(
                entry,
                tool_call_map,
                files_modified,
                tools_used,
            )

    print()
    print("---")
    print()
    print("## Summary Statistics")
    print()
    print(f"- **Turns**: {turn_number}")
    if tools_used:
        total = sum(tools_used.values())
        print(f"- **Total Tool Calls**: {total}")
        tool_summary = ", ".join(
            f"{name} ({count})"
            for name, count in sorted(
                tools_used.items(),
                key=lambda x: -x[1],
            )
        )
        print(f"- **Tools Used**: {tool_summary}")
    if files_modified:
        n = len(files_modified)
        print(f"- **Files Modified**: {n}")
        for fp in sorted(files_modified):
            print(f"  - `{fp}`")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Summarize a Claude Code session transcript"
            " in readable markdown."
        ),
    )
    parser.add_argument(
        "identifier",
        nargs="?",
        default=None,
        help=(
            "Session UUID, agent ID, or path to a"
            " .jsonl transcript file"
        ),
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Summarize the most recent session",
    )
    args = parser.parse_args()

    if args.latest or args.identifier is None:
        path = find_latest_transcript()
        if path is None:
            print(
                "Error: No session transcripts found"
                " under ~/.claude/projects/",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        path = resolve_transcript(args.identifier)
        if path is None:
            print(
                "Error: Could not find transcript"
                f" for '{args.identifier}'",
                file=sys.stderr,
            )
            print(
                "Provide a session UUID, agent ID,"
                " team-name/agent-name, or"
                " path to a .jsonl file.",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"Transcript: {path}", file=sys.stderr)
    print(file=sys.stderr)

    summarize_transcript(path)


if __name__ == "__main__":
    main()
