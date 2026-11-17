#!/usr/bin/env python3
"""Pass through bd and record calls in clones with .git/enact/bd-record/."""

import os
import signal
import stat
import subprocess
import sys
import time

# The Mac installer fills this in so the entry point needs no shell launcher.
PINNED_BINARY = None


def recording_directory(cwd):
    """Find the common Git directory without starting another process."""
    join = os.path.join
    if common := os.environ.get("GIT_COMMON_DIR"):
        return active_directory(common)
    git = os.environ.get("GIT_DIR")
    if git is None:
        parent = cwd
        while True:
            git = join(parent, ".git")
            if os.path.isdir(git):
                break
            if os.path.isfile(git):
                with open(git, encoding="utf-8", errors="surrogateescape") as pointer:
                    path = pointer.read().strip()
                if not path.startswith("gitdir: "):
                    return None
                git = join(parent, path.removeprefix("gitdir: "))
                break
            above = os.path.dirname(parent)
            if above == parent:
                return None
            parent = above
    common_file = join(git, "commondir")
    common = git
    if os.path.isfile(common_file):
        with open(common_file, encoding="utf-8", errors="surrogateescape") as pointer:
            common = join(git, pointer.read().strip())
    return active_directory(common)


def active_directory(common):
    path = os.path.join(common, "enact", "bd-record")
    return path if os.path.isdir(path) else None


def forward(source, destination, captured):
    """Forward bytes as they arrive, closing the child pipe on downstream EOF."""
    try:
        while chunk := os.read(source, 65536):
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination, remaining)
                captured.extend(remaining[:written])
                remaining = remaining[written:]
    except OSError:
        # For example head(1) can close the output pipe while bd is writing.
        pass


def append_record(path, record):
    import gzip
    import json

    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("ascii")
    # Each append is an independent gzip member. Repeated full-store exports
    # would otherwise write tens of gigabytes in a busy day.
    compressed = gzip.compress(line, compresslevel=1, mtime=0)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    try:
        # One append write per record so concurrent wrapper processes can share the file.
        os.write(fd, compressed)
    finally:
        os.close(fd)


def run(binary, argv):
    # An argument vector or executable failure from bd is still reported by bd.
    # Discovery and appending are optional, even if the state directory disappears.
    command = [binary, *argv]
    try:
        cwd = os.getcwd()
        directory = recording_directory(cwd)
    except (OSError, ValueError):
        directory = None
    if directory is None:
        os.execv(binary, command)

    import threading

    started = time.time_ns()
    try:
        descriptors = [os.fstat(fd) for fd in range(3)]
    except OSError:
        os.execv(binary, command)
    input_mode = descriptors[0].st_mode
    pipe_input = stat.S_ISFIFO(input_mode) or stat.S_ISSOCK(input_mode)
    stdin_offset = None
    if stat.S_ISREG(input_mode) and hasattr(os, "pread"):
        stdin_offset = os.lseek(0, 0, os.SEEK_CUR)
    # Terminal streams stay attached to the terminal. In particular, piping
    # stdout changes color and prompting, and piping stdin disables prompts.
    terminal_input = os.isatty(0)
    capture_stdout, capture_stderr = (not os.isatty(fd) for fd in (1, 2))
    stdout, stderr = descriptors[1:]
    same_output = bool(stdout.st_ino) and (
        (stdout.st_dev, stdout.st_ino) == (stderr.st_dev, stderr.st_ino)
    )
    merged = same_output and stat.S_ISFIFO(stdout.st_mode)
    if merged:
        capture_stderr = False
    elif same_output and stat.S_ISREG(stdout.st_mode):
        # Independent descriptors for one file can have different offsets.
        capture_stdout = capture_stderr = False
    input_bytes, output_bytes, error_bytes = bytearray(), bytearray(), bytearray()

    def pump_input(pipe):
        with pipe:
            forward(0, pipe.fileno(), input_bytes)

    def pump_output(pipe, target, captured):
        with pipe:
            forward(pipe.fileno(), target, captured)

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if pipe_input else None,
        stdout=subprocess.PIPE if capture_stdout else None,
        stderr=subprocess.STDOUT if merged else subprocess.PIPE if capture_stderr else None,
        close_fds=False,
    )
    if process.stdin is not None:
        # A writer may hold stdin open even when bd never reads from it.
        threading.Thread(target=pump_input, args=(process.stdin,), daemon=True).start()
    outputs = []
    for fd, pipe, captured in ((1, process.stdout, output_bytes), (2, process.stderr, error_bytes)):
        if pipe is not None:
            thread = threading.Thread(target=pump_output, args=(pipe, fd, captured))
            thread.start()
            outputs.append(thread)

    def relay(signum, _frame):
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass

    # Relay signals sent directly to the wrapper by timeout and job tools.
    original_handlers = {}
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            original_handlers[signum] = signal.signal(signum, relay)
    code = process.wait()
    for thread in outputs:
        thread.join()
    for signum, handler in original_handlers.items():
        signal.signal(signum, handler)

    try:
        if stdin_offset is not None:
            consumed = os.lseek(0, 0, os.SEEK_CUR) - stdin_offset
            input_bytes = os.pread(0, max(consumed, 0), stdin_offset)
        streams = [input_bytes, output_bytes, error_bytes]
        missing = [terminal_input, not capture_stdout, not capture_stderr]
        stdin, stdout, stderr = (
            None if omitted else value.decode("utf-8", "surrogateescape")
            for value, omitted in zip(streams, missing)
        )
        append_record(os.path.join(directory, "calls.jsonl.gz"), {
            "argv": argv, "cwd": cwd, "stdin": stdin, "stdout": stdout, "stderr": stderr,
            "returncode": code, "start_ns": started, "duration_ns": time.time_ns() - started,
        })
    except (OSError, ValueError, MemoryError):
        pass
    if code < 0:
        signum = -code
        if signum not in (getattr(signal, "SIGKILL", None), getattr(signal, "SIGSTOP", None)):
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    return code


if __name__ == "__main__":
    if PINNED_BINARY is None:
        sys.exit(run(sys.argv[1], sys.argv[2:]))
    sys.exit(run(PINNED_BINARY, sys.argv[1:]))
