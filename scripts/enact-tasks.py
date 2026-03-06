#!/usr/bin/env python3
"""Manage markdown-based task files with YAML frontmatter.

Usage:
    enact-tasks.py <tasks_dir> next-id
    enact-tasks.py <tasks_dir> list [--status S] [--tags T]
    enact-tasks.py <tasks_dir> available
    enact-tasks.py <tasks_dir> update <id> [--status S]
        [--owner O]
"""

import os
import re
import sys


def parse_frontmatter(text):
    """Parse YAML frontmatter from markdown text.

    Handles string, int, and list values. No external
    dependencies.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break

    if end is None:
        return {}, text

    fm = {}
    for line in lines[1:end]:
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        fm[key] = parse_value(val)

    body = "\n".join(lines[end + 1:])
    return fm, body


def parse_value(val):
    """Parse a single YAML value: int, list, or string."""
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        parts = [p.strip() for p in inner.split(",")]
        result = []
        for p in parts:
            p = strip_quotes(p)
            try:
                result.append(int(p))
            except ValueError:
                result.append(p)
        return result

    val = strip_quotes(val)

    try:
        return int(val)
    except ValueError:
        pass

    return val


def strip_quotes(s):
    """Remove surrounding single or double quotes."""
    if len(s) >= 2:
        if (s[0] == '"' and s[-1] == '"') or \
           (s[0] == "'" and s[-1] == "'"):
            return s[1:-1]
    return s


def extract_subject(body):
    """Extract the first H1 heading from the markdown body."""
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def load_tasks(tasks_dir):
    """Load all task files from the directory."""
    tasks = []
    if not os.path.isdir(tasks_dir):
        return tasks

    for fname in os.listdir(tasks_dir):
        match = re.match(r"^task_(\d+)\.md$", fname)
        if not match:
            continue
        path = os.path.join(tasks_dir, fname)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()

        fm, body = parse_frontmatter(text)
        task = {
            "id": fm.get("id", 0),
            "status": fm.get("status", ""),
            "owner": fm.get("owner", ""),
            "tags": fm.get("tags", ""),
            "blocked_by": fm.get("blocked_by", []),
            "subject": extract_subject(body),
        }
        tasks.append(task)

    tasks.sort(key=lambda t: t["id"])
    return tasks


def format_blocked_by(blocked_by):
    """Format blocked_by list for display."""
    if not blocked_by:
        return ""
    items = ", ".join(str(x) for x in blocked_by)
    return f"[{items}]"


def print_table(tasks):
    """Print tasks in an aligned table."""
    headers = [
        "ID", "Status", "Owner", "Tags",
        "BlockedBy", "Subject",
    ]

    rows = []
    for t in tasks:
        rows.append([
            str(t["id"]),
            str(t["status"]),
            str(t["owner"]),
            str(t["tags"]),
            format_blocked_by(t["blocked_by"]),
            str(t["subject"]),
        ])

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells):
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    print(fmt_row(headers))
    for row in rows:
        print(fmt_row(row))


def cmd_next_id(tasks_dir):
    """Print the next available task ID."""
    tasks = load_tasks(tasks_dir)
    if not tasks:
        print(1)
    else:
        max_id = max(t["id"] for t in tasks)
        print(max_id + 1)


def cmd_list(tasks_dir, status=None, tags=None):
    """List tasks, optionally filtered by status or tags."""
    tasks = load_tasks(tasks_dir)

    if status:
        tasks = [
            t for t in tasks if t["status"] == status
        ]
    if tags:
        tasks = [t for t in tasks if t["tags"] == tags]

    if not tasks:
        print("No tasks found.")
        return

    print_table(tasks)


def cmd_available(tasks_dir):
    """List pending, unowned, unblocked tasks."""
    all_tasks = load_tasks(tasks_dir)

    completed_ids = set()
    for t in all_tasks:
        if t["status"] == "completed":
            completed_ids.add(t["id"])

    available = []
    for t in all_tasks:
        if t["status"] != "pending":
            continue
        if t["owner"] not in ("", None):
            continue
        blocked = t.get("blocked_by", [])
        if blocked:
            all_done = all(
                b in completed_ids for b in blocked
            )
            if not all_done:
                continue
        available.append(t)

    if not available:
        print("No available tasks.")
        return

    print_table(available)


def resolve_task_path(tasks_dir, task_id):
    """Find the actual task file, trying both padded
    and unpadded names (e.g. task_1.md, task_01.md).
    """
    candidates = [
        f"task_{task_id}.md",
        f"task_{task_id:02d}.md",
    ]
    for name in candidates:
        path = os.path.join(tasks_dir, name)
        if os.path.isfile(path):
            return path
    return None


def cmd_update(tasks_dir, task_id, status=None,
               owner=None):
    """Update a task file's frontmatter fields."""
    path = resolve_task_path(tasks_dir, task_id)
    if path is None:
        print(
            f"Error: task file for ID {task_id} "
            "not found.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        print(
            "Error: no YAML frontmatter found.",
            file=sys.stderr,
        )
        sys.exit(1)

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break

    if end is None:
        print(
            "Error: unterminated frontmatter.",
            file=sys.stderr,
        )
        sys.exit(1)

    updates = {}
    if status is not None:
        updates["status"] = status
    if owner is not None:
        updates["owner"] = owner

    if not updates:
        print(
            "Error: no fields to update. "
            "Use --status and/or --owner.",
            file=sys.stderr,
        )
        sys.exit(1)

    for i in range(1, end):
        line = lines[i]
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, _, _ = stripped.partition(":")
        key = key.strip()
        if key in updates:
            val = updates[key]
            if val == "":
                lines[i] = f'{key}: ""'
            else:
                lines[i] = f"{key}: {val}"
            del updates[key]

    # Add any fields that didn't exist yet
    for key, val in updates.items():
        if val == "":
            lines.insert(end, f'{key}: ""')
        else:
            lines.insert(end, f"{key}: {val}")
        end += 1

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Updated task {task_id}.")


def print_usage():
    """Print usage help."""
    print(
        "Usage:\n"
        "  enact-tasks.py <tasks_dir> next-id\n"
        "  enact-tasks.py <tasks_dir> list"
        " [--status S] [--tags T]\n"
        "  enact-tasks.py <tasks_dir> available\n"
        "  enact-tasks.py <tasks_dir> update <id>"
        " [--status S] [--owner O]\n"
        "\n"
        "Commands:\n"
        "  next-id    Print next available task ID\n"
        "  list       List tasks (filterable)\n"
        "  available  List pending unblocked tasks\n"
        "  update     Update task frontmatter fields"
    )


def main():
    args = sys.argv[1:]

    if not args or args[0] == "--help":
        print_usage()
        sys.exit(0)

    if len(args) < 2:
        print_usage()
        sys.exit(1)

    tasks_dir = args[0]
    command = args[1]
    rest = args[2:]

    if not os.path.isdir(tasks_dir):
        print(
            f"Error: directory '{tasks_dir}' "
            "does not exist.",
            file=sys.stderr,
        )
        sys.exit(1)

    if command == "next-id":
        cmd_next_id(tasks_dir)
    elif command == "list":
        status = None
        tags = None
        i = 0
        while i < len(rest):
            if rest[i] == "--status" and i + 1 < len(rest):
                status = rest[i + 1]
                i += 2
            elif rest[i] == "--tags" and i + 1 < len(rest):
                tags = rest[i + 1]
                i += 2
            else:
                i += 1
        cmd_list(tasks_dir, status=status, tags=tags)
    elif command == "available":
        cmd_available(tasks_dir)
    elif command == "update":
        if not rest:
            print(
                "Error: update requires a task ID.",
                file=sys.stderr,
            )
            print_usage()
            sys.exit(1)
        try:
            task_id = int(rest[0])
        except ValueError:
            print(
                f"Error: invalid task ID '{rest[0]}'.",
                file=sys.stderr,
            )
            sys.exit(1)
        upd_status = None
        upd_owner = None
        i = 1
        while i < len(rest):
            if (rest[i] == "--status"
                    and i + 1 < len(rest)):
                upd_status = rest[i + 1]
                i += 2
            elif (rest[i] == "--owner"
                    and i + 1 < len(rest)):
                upd_owner = rest[i + 1]
                i += 2
            else:
                i += 1
        cmd_update(
            tasks_dir, task_id,
            status=upd_status, owner=upd_owner,
        )
    else:
        print(
            f"Error: unknown command '{command}'",
            file=sys.stderr,
        )
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
