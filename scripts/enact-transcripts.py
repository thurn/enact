#!/usr/bin/env python3
"""Find and output all transcripts for a given Enact session.

Given an enact ID, finds the orchestrator session, all subagent
transcripts, and all team member sessions, then outputs their paths
(or contents) to stdout.

Usage:
    enact-transcripts.py [enact_id]
        List transcripts with agent labels
        (latest session if omitted)
    enact-transcripts.py --cat [enact_id]     # output transcript contents
    enact-transcripts.py --paths [enact_id]   # list bare paths only
"""

import argparse
import json
import re
import sys
from pathlib import Path


def find_latest_enact_id() -> str | None:
    """Find the most recent enact ID (highest numeric directory name)."""
    enact_dir = Path.home() / ".llms" / "enact"
    if not enact_dir.is_dir():
        return None
    candidates = []
    for d in enact_dir.iterdir():
        if d.is_dir() and d.name.isdigit():
            candidates.append(int(d.name))
    if not candidates:
        return None
    return str(max(candidates))


def find_project_dirs() -> list[Path]:
    """Find all project directories under ~/.claude/projects/."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []
    return [d for d in projects_dir.iterdir() if d.is_dir()]


def find_orchestrator_session(
    enact_id: str, project_dirs: list[Path]
) -> tuple[Path, Path] | None:
    """Find the orchestrator session that contains the enact ID.

    Returns (project_dir, session_jsonl_path) or None.
    Identifies the orchestrator by finding sessions that both reference the
    enact ID AND have a subagents/ directory (i.e., they spawned subagents).
    Falls back to any session referencing the enact ID if no subagent dir found.
    """
    candidates_with_subagents: list[tuple[Path, Path, int]] = []
    candidates_without: list[tuple[Path, Path]] = []

    for project_dir in project_dirs:
        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            session_id = jsonl_file.stem
            session_dir = project_dir / session_id

            try:
                with open(jsonl_file) as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue

            if enact_id not in content:
                continue

            has_subagents = (session_dir / "subagents").is_dir()
            entry = (project_dir, jsonl_file)

            if has_subagents:
                subagent_count = len(
                    list((session_dir / "subagents").glob("agent-*.jsonl"))
                )
                candidates_with_subagents.append((*entry, subagent_count))
            else:
                candidates_without.append(entry)

    if candidates_with_subagents:
        candidates_with_subagents.sort(key=lambda x: x[2], reverse=True)
        return (
            candidates_with_subagents[0][0],
            candidates_with_subagents[0][1],
        )

    if candidates_without:
        return candidates_without[0]

    return None


def build_agent_label_map(orchestrator_jsonl: Path) -> dict[str, str]:
    """Build a map of agentId -> description from the orchestrator transcript.

    Parses Task tool_use calls and their corresponding tool_result responses
    to match each agentId to the short description given when it was spawned.
    """
    label_map: dict[str, str] = {}
    pending: dict[str, str] = {}

    try:
        with open(orchestrator_jsonl) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if d.get("type") == "assistant" and "message" in d:
                    for c in d["message"].get("content", []):
                        if (
                            isinstance(c, dict)
                            and c.get("type") == "tool_use"
                            and c.get("name") == "Task"
                        ):
                            pending[c["id"]] = c.get("input", {}).get(
                                "description", ""
                            )

                elif d.get("type") == "user" and "message" in d:
                    content = d["message"].get("content", [])
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") != "tool_result":
                            continue
                        tool_use_id = c.get("tool_use_id", "")
                        if tool_use_id not in pending:
                            continue
                        text = ""
                        for part in c.get("content", []):
                            if isinstance(part, dict):
                                text += part.get("text", "")
                        m = re.search(r"agentId: (a[0-9a-f]+)", text)
                        if m:
                            label_map[m.group(1)] = pending[tool_use_id]
    except (OSError, UnicodeDecodeError):
        pass

    return label_map


def get_start_timestamp(transcript_path: Path) -> str:
    """Extract the earliest timestamp from a transcript for sorting."""
    try:
        with open(transcript_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "timestamp" in d:
                    return d["timestamp"]
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def get_team_info(transcript_path: Path) -> tuple[str, str] | None:
    """Extract teamName and agentName from a team member transcript.

    Returns (teamName, agentName) or None if not a team member session.
    """
    try:
        with open(transcript_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                team = d.get("teamName")
                agent = d.get("agentName")
                if team and agent:
                    return (team, agent)
    except (OSError, UnicodeDecodeError):
        pass
    return None


def find_team_member_sessions(
    enact_id: str, project_dirs: list[Path], orchestrator_jsonl: Path
) -> list[tuple[Path, str, str]]:
    """Find all team member sessions for the given enact ID.

    Team members are sessions in project directories whose teamName field
    starts with '<enact_id>-'. Returns a list of (path, teamName, agentName)
    sorted by start timestamp.
    """
    prefix = f"{enact_id}-"
    orchestrator_path = str(orchestrator_jsonl)
    results: list[tuple[Path, str, str]] = []

    for project_dir in project_dirs:
        for jsonl_file in project_dir.glob("*.jsonl"):
            if str(jsonl_file) == orchestrator_path:
                continue

            info = get_team_info(jsonl_file)
            if info is None:
                continue

            team_name, agent_name = info
            if team_name.startswith(prefix):
                results.append((jsonl_file, team_name, agent_name))

    results.sort(key=lambda x: get_start_timestamp(x[0]))
    return results


def collect_transcripts(
    project_dir: Path, orchestrator_jsonl: Path
) -> list[Path]:
    """Collect the orchestrator transcript and all direct subagent transcripts,
    sorted by start timestamp."""
    transcripts = [orchestrator_jsonl]

    session_dir = project_dir / orchestrator_jsonl.stem
    subagents_dir = session_dir / "subagents"
    if subagents_dir.is_dir():
        subagent_files = list(subagents_dir.glob("agent-*.jsonl"))
        subagent_files.sort(key=get_start_timestamp)
        transcripts.extend(subagent_files)

    return transcripts


def cat_transcript(transcript_path: Path) -> None:
    """Output the raw contents of a transcript to stdout."""
    try:
        with open(transcript_path) as f:
            for line in f:
                sys.stdout.write(line)
    except (OSError, UnicodeDecodeError) as e:
        print(f"Error reading {transcript_path}: {e}", file=sys.stderr)


def format_team_label(team_name: str, agent_name: str, enact_id: str) -> str:
    """Format a human-readable label for a team member.

    Strips the enact_id prefix from the team name for readability.
    E.g., '1771028742-review-foundation' + 'reviewer-1' ->
          '[team: review-foundation] reviewer-1'
    """
    prefix = f"{enact_id}-"
    short_team = team_name.removeprefix(prefix)
    return f"[team: {short_team}] {agent_name}"


def main():
    parser = argparse.ArgumentParser(
        description="Find and output all transcripts for an Enact session."
    )
    parser.add_argument(
        "enact_id",
        nargs="?",
        default=None,
        help="The enact session ID (defaults to most recent)",
    )
    parser.add_argument(
        "--cat",
        action="store_true",
        help="Output transcript contents instead of paths",
    )
    parser.add_argument(
        "--paths",
        action="store_true",
        help="Output bare paths only (no labels)",
    )
    args = parser.parse_args()

    enact_id = args.enact_id
    if enact_id is None:
        enact_id = find_latest_enact_id()
        if enact_id is None:
            print(
                "Error: No enact sessions found under ~/.llms/enact/",
                file=sys.stderr,
            )
            sys.exit(1)

    enact_dir = Path.home() / ".llms" / "enact" / enact_id
    if not enact_dir.is_dir():
        print(
            f"Error: Enact scratch directory not found: {enact_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    project_dirs = find_project_dirs()
    if not project_dirs:
        print(
            "Error: No project directories found under ~/.claude/projects/",
            file=sys.stderr,
        )
        sys.exit(1)

    result = find_orchestrator_session(enact_id, project_dirs)
    if result is None:
        print(
            "Error: No session transcript found "
            f"containing enact ID '{enact_id}'",
            file=sys.stderr,
        )
        sys.exit(1)

    project_dir, orchestrator_jsonl = result
    session_id = orchestrator_jsonl.stem

    print(f"Enact ID: {enact_id}", file=sys.stderr)
    print(f"Session ID: {session_id}", file=sys.stderr)
    print(f"Project dir: {project_dir}", file=sys.stderr)

    transcripts = collect_transcripts(project_dir, orchestrator_jsonl)
    label_map = build_agent_label_map(orchestrator_jsonl)
    team_members = find_team_member_sessions(
        enact_id, project_dirs, orchestrator_jsonl
    )

    # Build a label map for team member paths
    team_label_map: dict[str, str] = {}
    for path, team_name, agent_name in team_members:
        team_label_map[str(path)] = format_team_label(
            team_name, agent_name, enact_id
        )

    # Combine all transcripts
    all_transcripts = list(transcripts)
    team_paths = [t[0] for t in team_members]
    all_transcripts.extend(team_paths)

    subagent_count = len(transcripts) - 1
    team_count = len(team_members)
    print(
        f"Found {len(all_transcripts)} transcripts "
        f"({subagent_count} subagents, {team_count} team members)",
        file=sys.stderr,
    )
    print(file=sys.stderr)

    if args.cat:
        for t in all_transcripts:
            print(f"=== {t} ===", file=sys.stderr)
            cat_transcript(t)
    elif args.paths:
        for t in all_transcripts:
            print(t)
    else:
        # Print orchestrator and direct subagents
        for i, t in enumerate(transcripts):
            if i == 0:
                label = "Orchestrator"
            else:
                agent_id = t.stem.removeprefix("agent-")
                label = label_map.get(agent_id, agent_id)

            if i > 0:
                print()
            print(label)
            print(t)

        # Print team members grouped by team
        if team_members:
            # Group by team name, preserving timestamp order within groups
            teams: dict[str, list[tuple[Path, str]]] = {}
            for path, team_name, agent_name in team_members:
                short_team = team_name.removeprefix(f"{enact_id}-")
                if short_team not in teams:
                    teams[short_team] = []
                teams[short_team].append((path, agent_name))

            # Print each team
            for team_short_name, members in teams.items():
                print()
                print(f"--- Team: {team_short_name} ---")
                for path, agent_name in members:
                    print()
                    print(f"  {agent_name}")
                    print(f"  {path}")


if __name__ == "__main__":
    main()
