#!/usr/bin/env python3
"""Select one recorded bd call per command and flag combination."""

import argparse
from collections import Counter
from functools import cache
import gzip
import json
from pathlib import Path
import re
import subprocess

# These commands cover the compatibility surface during the ticket cutover.
COMMANDS = {
    "close", "comments", "create", "defer", "dep", "dolt", "epic", "export",
    "init", "label", "list", "ready", "reopen", "show", "undefer", "update",
}
FLAG = re.compile(r"^ *(?:(-[A-Za-z]), )?(--[\w-]+)(?: ([^ ]+))? {2,}")
SUBCOMMAND = re.compile(r"^  ([\w-]+) +")


def arguments(argv, takes_value):
    """Yield (flag, None) or (None, positional), skipping values of known flags."""
    options = iter(argv)
    for arg in options:
        if arg == "--":
            for positional in options:
                yield None, positional
            break
        if arg.startswith("--"):
            name, equals, _ = arg.partition("=")
            yield name, None
            if not equals and takes_value.get(name):
                next(options, None)
        elif arg.startswith("-") and len(arg) > 1 and not arg[1].isdigit():
            for index, letter in enumerate(arg[1:], start=1):
                name = "-" + letter
                yield name, None
                if takes_value.get(name):
                    if index == len(arg) - 1:
                        next(options, None)
                    break
                if arg[index + 1:index + 2] == "=":
                    break
        else:
            yield None, arg


class Help:
    def __init__(self, binary):
        self.binary = binary

    @cache
    def command(self, command):
        """Read flag arity and child names from the version that made the log."""
        result = subprocess.run(
            [self.binary, *command, "--help"], check=True, capture_output=True, text=True,
        )
        flags = {}
        children = set()
        section = ""
        for line in result.stdout.splitlines():
            if not line.startswith(" "):
                section = line
            if section in {"Flags:", "Global Flags:"} and (match := FLAG.match(line)):
                short, long, value = match.groups()
                flags[long] = value is not None
                if short:
                    flags[short] = value is not None
            if section == "Available Commands:" and (match := SUBCOMMAND.match(line)):
                children.add(match[1])
        return flags, children


def signature(argv, help):
    root_flags, _ = help.command(())
    positionals = (value for flag, value in arguments(argv, root_flags) if flag is None)
    command = (next(positionals, ""),)
    if command[0] not in COMMANDS:
        return None
    flags, children = help.command(command)
    while children:
        positionals = (value for flag, value in arguments(argv, flags) if flag is None)
        for _ in command:
            next(positionals, None)
        child = next(positionals, None)
        if child not in children:
            break
        command += (child,)
        flags, children = help.command(command)
    used = tuple(sorted(flag for flag, _ in arguments(argv, flags) if flag is not None))
    return command, used


def open_log(path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="ascii")
    return path.open(encoding="ascii")


def read_calls(path):
    try:
        with open_log(path) as log:
            for line in log:
                if line.endswith("\n"):
                    yield json.loads(line)
    except EOFError:
        # A scan of the live log can end during the final gzip member.
        pass


def reduce(sources, help, since=None, before=None):
    cases = {}
    for source, root, path in sources:
        for call in read_calls(path):
            started = call["start_ns"]
            if since is not None and started < since:
                continue
            if before is not None and started >= before:
                continue
            key = signature(call["argv"], help)
            if key is None:
                continue
            old = cases.get(key)
            rank = (call["returncode"] != 0, call["stdout"] is None, started)
            if old is not None and old[0] <= rank:
                continue
            case = {
                "command": key[0], "flags": key[1], "source": source,
                **call, "cwd": str(Path(call["cwd"]).relative_to(root)),
            }
            cases[key] = rank, case
    return [case for _, case in sorted(cases.values(), key=lambda entry: entry[1]["start_ns"])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-bd", required=True)
    parser.add_argument("--source", nargs=3, action="append", required=True,
                        metavar=("NAME", "CLONE", "LOG"))
    parser.add_argument("--since-ns", type=int)
    parser.add_argument("--before-ns", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    sources = [(name, Path(root).resolve(), Path(log)) for name, root, log in args.source]
    cases = reduce(sources, Help(args.real_bd), args.since_ns, args.before_ns)
    lines = [json.dumps(case, separators=(",", ":")) for case in cases]
    payload = ("\n".join(lines) + ("\n" if lines else "")).encode("ascii")
    if args.output.suffix == ".gz":
        payload = gzip.compress(payload, mtime=0)
    args.output.write_bytes(payload)
    counts = Counter(" ".join(case["command"]) for case in cases)
    for command, count in sorted(counts.items()):
        print(f"{command}: {count}")
    flags = {flag for case in cases for flag in case["flags"]}
    print(f"{len(counts)} distinct command paths, {len(flags)} distinct flags")
    print(f"{len(cases)} distinct command and flag combinations")


if __name__ == "__main__":
    main()
