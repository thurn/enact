#!/usr/bin/env python3
"""Enact helper: git and Beads plumbing for the Enact orchestration framework.

Stdlib only, Python 3.11+, macOS, Linux and Windows. Every subcommand prints
exactly one JSON object on stdout; progress and diagnostics go to stderr.

Exit codes: 0 success, 1 failure, 2 the agent must act (for example resolve a
rebase conflict), 3 lost a race (claim or lock held by someone else).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import functools
import hashlib
import json
import os
import random
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO



def stable_checkout(root: Path) -> Path:
    """The Enact checkout live sessions run from, even when `root` is one of
    its delivery worktrees, which `enact push` deletes."""
    return root.parent.parent if root.parent.name == ".worktrees" else root


ENACT_ROOT = Path(__file__).resolve().parent.parent
# Prompts name role files by path, and a session reads them after the
# delivering worktree is gone, so they point at the stable checkout.
ROLES_DIR = stable_checkout(ENACT_ROOT) / "skills" / "enact" / "roles"
JUSTICIAR_PREFIX = "refs/enact/justiciars/"  # one lock per machine
LEGACY_JUSTICIAR_REF = "refs/enact/justiciar"  # the old fleet-wide lock
EMOJI = {
    "vizier": "🔮",
    "executor": "⚒️",
    "warden": "🛡️",
    "justiciar": "🔥",
    "steward": "🚒",
    "mason": "🧱",
    "sage": "📖",
    "archivist": "📜",
}
SPAWNED_ROLES = ("executor", "warden", "steward", "mason", "sage", "archivist", "justiciar")
# Users read status from the title emoji, so every turn ends on ✅, ⛔ or 🔍.
TITLE_NOTICE = (
    "Keep this session's title showing its state: `⚒️ <short summary>` while you "
    "work (`set_session_title` on `self` in Claude Desktop, `set_thread_title` in "
    "Codex), then `✅ <same summary>` as your last step when finished, "
    "`⛔ <same summary>` if you stop without finishing, or `🔍 <same summary>` "
    "when your turn ends waiting on the user's answer or decision."
)
WINDOWS = os.name == "nt"
# Seconds a directory git does not track must sit untouched before the
# worktree sweep deletes it, so it never races a tree being created.
SWEEP_AGE = 3600
# Seconds a vizier's lease stays fresh after its last `enact next` step.
# `next` renews it on every poll (`--interval`, a minute by default), so this
# only has to cover the gaps while the vizier acts on a step between calls.
VIZIER_LEASE_SECONDS = 20 * 60


class Fail(Exception):
    def __init__(self, message: str, code: int = 1, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def log(message: str) -> None:
    print(f"enact: {message}", file=sys.stderr, flush=True)


def write_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value), encoding="utf-8")
    temp.replace(path)


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    cmd: list[str],
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=full_env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # A fresh clone has no local Beads database until `enact setup`
        # bootstraps it from the remote; a fresh machine may lack the tool.
        if not cwd.is_dir():
            raise Fail(f"{cwd} does not exist; on a fresh clone, run "
                       "`enact setup` to bootstrap the Beads database") from None
        raise Fail(f"{cmd[0]} is not installed or not on PATH; "
                   "see the README's requirements") from None
    if check and proc.returncode != 0:
        raise Fail(
            f"{' '.join(cmd)} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[-2000:]}"
        )
    return proc


def git_bash() -> str:
    """Git for Windows' bash: `ENACT_BASH`, else the `bin/bash.exe` of the git on PATH."""
    if os.environ.get("ENACT_BASH"):
        return os.environ["ENACT_BASH"]
    roots = []
    if git := shutil.which("git"):
        roots += list(Path(git).resolve().parents)[:3]  # <root>/cmd/git.exe, <root>/mingw64/bin/git.exe
    roots.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git")
    for root in roots:
        # bin/bash.exe, not usr/bin/bash.exe: the launcher puts usr/bin and mingw64/bin on PATH.
        if (root / "bin" / "bash.exe").is_file() and (root / "usr" / "bin").is_dir():
            return str(root / "bin" / "bash.exe")
    raise Fail("project commands need Git for Windows' bash; install Git for Windows or set ENACT_BASH")


def shell_args(command: str) -> tuple[str | list[str], bool]:
    """subprocess `(args, shell)` for a project-configured command (.enact.toml).

    The commands are POSIX shell, so on Windows they run in Git for Windows' bash rather than
    cmd.exe, which `shell=True` would use.
    """
    if WINDOWS:
        return [git_bash(), "-c", command], False
    return command, True


def run_shell(command: str, cwd: Path, env: dict[str, str] | None = None) -> int:
    """Run a project-configured shell command, streaming output to stderr."""
    log(f"$ {command}")
    args, shell = shell_args(command)
    proc = subprocess.run(
        args,
        shell=shell,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=sys.stderr,
        stderr=sys.stderr,
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode


def host_name() -> str:
    return os.environ.get("ENACT_HOST") or socket.gethostname().split(".")[0].lower()


def provider() -> str:
    if os.environ.get("ENACT_PROVIDER"):
        return os.environ["ENACT_PROVIDER"]
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    return "unknown"


def session_actor() -> str | None:
    for var in ("ENACT_SESSION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        if os.environ.get(var):
            return os.environ[var]
    return None


def session_aliases() -> set[str]:
    """Every id that names this session. Claude Desktop's start_session returns
    the host session id, which the vizier records as enact_session, while
    session_actor() keeps the CLI id so live viziers keep their identity.
    ENACT_SESSION, when set, is the only name."""
    if os.environ.get("ENACT_SESSION"):
        return {os.environ["ENACT_SESSION"]}
    names = ("CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
    return {os.environ[v] for v in names if os.environ.get(v)}


def as_owner(actor: str, bead: dict[str, Any]) -> str:
    """The bead's assignee or bound session when it is another name for actor."""
    aliases = session_aliases()
    if actor in aliases:
        for name in (bead.get("assignee"), meta(bead, "enact_session"),
                     meta(bead, "enact_pool_session")):
            if name and name in aliases:
                return name
    return actor


def require_actor(explicit: str | None) -> str:
    actor = explicit or session_actor()
    if not actor:
        raise Fail(
            "no session identity: pass --actor or run inside a Claude Code or "
            "Codex session (CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID)"
        )
    return actor


# ---------------------------------------------------------------------------
# Repository and configuration


# Worktrees live inside the main checkout, ignored by git.
DEFAULT_WORKTREE_ROOT = ".worktrees"
# Tiers every project has without configuring them: a Claude model by name, so a plan can
# assign beads to models with `tier:fable`, `tier:opus` and `tier:sonnet`. A project's own
# `[tiers.<name>.<provider>]` overrides one; another provider's sessions keep their role's
# settings unless the project maps the tier for them.
BUILTIN_TIERS: dict[str, dict[str, dict[str, str]]] = {
    "fable": {"claude": {"model": "fable"}},
    "opus": {"claude": {"model": "opus"}},
    "sonnet": {"claude": {"model": "sonnet"}},
}
# Far above the ~5 minute slow-check budget, so only a hang reaches it.
DEFAULT_SLOW_TIMEOUT = 3600
# The merge queue's largest batch and its fast check's deadline. A hung check would hold every
# push on the host, so unlike a lone push's check it is killed.
DEFAULT_PUSH_BATCH = 8
DEFAULT_PUSH_TIMEOUT = 1800


@dataclass(frozen=True)
class Config:
    remote: str = "origin"
    staging: str = "staging"
    master: str = "master"
    fast: str | None = None
    slow: str | None = None
    # A hung slow check must fail, not hold master back forever (a stuck exec once held it
    # 42 minutes): the runner kills it after this many seconds.
    slow_timeout: int = DEFAULT_SLOW_TIMEOUT
    # The least time, in seconds, between the end of one slow-CI run and the start of the next.
    # Under a high commit rate the runner would otherwise be busy all the time and compete with
    # the sessions' builds; with a gap, commits that arrive meanwhile join the next run's batch.
    slow_min_gap: int = 0
    worktree_root: str = DEFAULT_WORKTREE_ROOT
    worktree_create: str | None = None
    worktree_remove: str | None = None
    # How many fixed `slot-NN` directories bead worktrees are drawn from; 0 gives each bead
    # a new directory, as before.
    worktree_slots: int = 0
    # `[push] queue = true` lands pushes through the host's merge queue (`run_queue`): one
    # fast check for a batch of up to `batch` branches. Off keeps one check per push.
    push_queue: bool = False
    push_batch: int = DEFAULT_PUSH_BATCH
    push_timeout: int = DEFAULT_PUSH_TIMEOUT
    roles: dict[str, Any] = field(default_factory=dict)
    tiers: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def parse(text: str) -> Config:
        data = tomllib.loads(text)
        checks = data.get("checks", {})
        worktree = data.get("worktree", {})
        push = data.get("push", {})
        return Config(
            remote=data.get("remote", "origin"),
            staging=data.get("staging_branch", "staging"),
            master=data.get("master_branch", "master"),
            fast=checks.get("fast") or None,
            slow=checks.get("slow") or None,
            slow_timeout=int(checks.get("slow_timeout") or DEFAULT_SLOW_TIMEOUT),
            slow_min_gap=max(int(checks.get("slow_min_gap") or 0), 0),
            worktree_root=worktree.get("root", DEFAULT_WORKTREE_ROOT),
            worktree_create=worktree.get("create") or None,
            worktree_remove=worktree.get("remove") or None,
            worktree_slots=int(worktree.get("slots") or 0),
            push_queue=bool(push.get("queue", False)),
            push_batch=max(1, int(push.get("batch") or DEFAULT_PUSH_BATCH)),
            push_timeout=int(push.get("timeout") or DEFAULT_PUSH_TIMEOUT),
            roles=data.get("roles", {}),
            tiers=data.get("tiers", {}),
        )

    def role_settings(self, role: str, host_provider: str,
                      tier: str | None = None) -> dict[str, str]:
        """Model settings for a spawn: the role's, overridden by the bead's
        `tier:<name>` label when that tier is built in or the project configures it."""
        settings: dict[str, str] = {}
        for table, key in ((self.roles, role), (BUILTIN_TIERS, tier), (self.tiers, tier)):
            entry = table.get(key, {}).get(host_provider, {}) if key else {}
            settings.update({k: v for k, v in entry.items() if k in ("model", "effort") and v})
        return settings


@dataclass(frozen=True)
class Repo:
    top: Path
    main: Path
    common: Path
    config: Config

    @property
    def state(self) -> Path:
        path = self.common / "enact"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def git(self, *args: str, check: bool = True, cwd: Path | None = None) -> str:
        return run(["git", *args], cwd or self.top, check=check).stdout.strip()

    def git_ok(self, *args: str, cwd: Path | None = None) -> bool:
        return run(["git", *args], cwd or self.top, check=False).returncode == 0

    def remote_ref(self, branch: str) -> str:
        return f"{self.config.remote}/{branch}"

    def worktree_root(self) -> Path:
        raw = self.config.worktree_root.replace("{repo}", self.main.name)
        root = Path(raw).expanduser()
        if not root.is_absolute():
            root = (self.main / root).resolve()
        return root

    def ignore_worktree_root(self, ignore_file: Path) -> str | None:
        """Make sure git ignores a worktree root inside the main checkout.

        Appends a pattern to `ignore_file` if needed and returns it.
        """
        try:
            rel = self.worktree_root().relative_to(self.main.resolve())
        except ValueError:
            return None  # outside the checkout
        return self.ignore(ignore_file, f"/{rel.as_posix()}/")

    def ignore(self, ignore_file: Path, pattern: str) -> str | None:
        """Append `pattern` to `ignore_file` unless git already ignores it."""
        if self.git_ok("check-ignore", "-q", "--no-index", pattern[1:], cwd=self.main):
            return None
        text = ignore_file.read_text(encoding="utf-8") if ignore_file.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        ignore_file.parent.mkdir(parents=True, exist_ok=True)
        # LF on Windows too: the file is committed and shared across machines.
        ignore_file.write_text(text + pattern + "\n", encoding="utf-8", newline="\n")
        return pattern


def find_repo(cwd: Path | None = None) -> Repo:
    cwd = cwd or Path.cwd()
    top = run(["git", "rev-parse", "--show-toplevel"], cwd).stdout.strip()
    common = run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd
    ).stdout.strip()
    top_path, common_path = Path(top), Path(common)
    main = common_path.parent if common_path.name == ".git" else common_path
    config = Config()
    for candidate in (top_path / ".enact.toml", main / ".enact.toml"):
        if candidate.is_file():
            config = Config.parse(candidate.read_text(encoding="utf-8"))
            break
    return Repo(top=top_path, main=main, common=common_path, config=config)


# ---------------------------------------------------------------------------
# Beads


class Beads:
    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self.env = {"BD_NON_INTERACTIVE": "1"}

    def raw(self, *args: str, actor: str | None = None) -> subprocess.CompletedProcess[str]:
        if args[0] not in {"show", "list", "ready"}:
            _beads_reads.get(self.repo.common, {}).clear()
        cmd = ["bd"]
        if actor:
            cmd += ["--actor", actor]
        return run([*cmd, *args], self.repo.main, check=False, env=self.env)

    def run(self, *args: str, actor: str | None = None) -> str:
        proc = self.raw(*args, actor=actor)
        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout).strip()
            if "schema version mismatch" in output:
                # Another machine's newer bd migrated the shared database.
                output += "\nupgrade bd to the newest Beads release"
            raise Fail(f"bd {' '.join(args)} failed: {output}")
        return proc.stdout

    def json(self, *args: str, actor: str | None = None) -> Any:
        out = self.run(*args, "--json", actor=actor)
        return json.loads(out) if out.strip() else None

    def read(self, *args: str) -> Any:
        # A helper-owned lock gives one coherent read phase. Writes and sync
        # invalidate it; unlocked observers always query Beads directly.
        cache = _beads_reads.get(self.repo.common)
        if cache is None:
            return self.json(*args)
        if args not in cache:
            cache[args] = self.run(*args, "--json")
        out = cache[args]
        return json.loads(out) if out.strip() else None

    def show(self, bead_id: str) -> dict[str, Any]:
        data = self.read("show", bead_id)
        return data[0] if isinstance(data, list) else data

    def all(self) -> list[dict[str, Any]]:
        return self.read("list", "--all", "-n", "0") or []

    def has_remote(self) -> bool:
        proc = self.raw("dolt", "remote", "list")
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def set_meta(self, bead_id: str, pairs: dict[str, str], actor: str | None = None) -> None:
        args = ["update", bead_id]
        for key, value in pairs.items():
            args += ["--set-metadata", f"{key}={value}"]
        self.run(*args, actor=actor)


def meta(bead: dict[str, Any], key: str) -> str | None:
    value = (bead.get("metadata") or {}).get(key)
    if isinstance(value, bool):  # bd stores key=true as a JSON boolean
        return "true" if value else "false"
    return None if value in (None, "") else str(value)


def label_value(bead: dict[str, Any], prefix: str) -> str | None:
    for label in bead.get("labels") or []:
        if label.startswith(prefix + ":"):
            return label.split(":", 1)[1]
    return None


def role_of(bead: dict[str, Any]) -> str:
    return label_value(bead, "role") or "executor"


def spawn_order(bead: dict[str, Any]) -> tuple[Any, ...]:
    """Stewards first, then priority, then lowest bead number (ps-e8r.9 < .10)."""
    number = [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", bead["id"])]
    return role_of(bead) != "steward", bead.get("priority", 2), number


def steward_targeted_here(bead: dict[str, Any]) -> bool:
    """Each machine's viziers repair only that machine's slow-CI failures. A
    failure elsewhere is left to its own machine's vizier, or to the session
    there that pushed and got `watch_ci`."""
    failed_host, failed_os = meta(bead, "enact_ci_host"), meta(bead, "enact_ci_os")
    if failed_host:
        return failed_host == host_name()
    # Stewards filed before CI recorded its host, or filed by hand.
    return not failed_os or failed_os == ci_os()


def targeted_here(bead: dict[str, Any]) -> bool:
    want_host = meta(bead, "enact_host")
    want_provider = meta(bead, "enact_provider")
    if not want_host and role_of(bead) == "steward" and not steward_targeted_here(bead):
        return False
    return (not want_host or want_host == host_name()) and (
        not want_provider or want_provider == provider()
    )


def stranded(repo: Repo, bead: dict[str, Any], epic: dict[str, Any]) -> bool:
    """A bead pinned to a host or provider that no vizier registered on the
    run serves. Nothing will pick it up, so it must not hold the run. A route
    on this machine serves only while its vizier's lease is fresh; another
    machine's leases cannot be read here, so its routes still count."""
    want_host, want_provider = meta(bead, "enact_host"), meta(bead, "enact_provider")
    if not want_host and not want_provider:
        return False
    routes = [route for route in run_routes(epic)
              if route.get("host") != host_name() or route_alive(repo, epic["id"], route)]
    return not any((not want_host or route.get("host") == want_host)
                   and (not want_provider or route.get("provider") == want_provider)
                   for route in routes)


def dolt_db_dir(repo: Repo) -> Path:
    metadata = json.loads((repo.main / ".beads" / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("dolt_mode", "embedded") != "embedded":
        raise Fail("automatic Dolt conflict resolution needs embedded mode")
    return repo.main / ".beads" / "embeddeddolt" / metadata["dolt_database"]


DOLT_BUSY_SECONDS = 60
# What Dolt says while another process has the store open: on Windows its
# file lock keeps it from opening the store at all, even read-only.
DOLT_BUSY = ("database is read only", "fslock")


def dolt(args: list[str], db: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run the Dolt CLI on an embedded Beads database. While another process
    has the store open (any bd command, even a read), Dolt opens it read-only
    instead of waiting and every write fails; bd waits, so retry like it."""
    deadline = time.monotonic() + DOLT_BUSY_SECONDS
    delay = 0.1
    while True:
        proc = run(["dolt", *args], db, check=False)
        output = proc.stdout + proc.stderr
        if (proc.returncode == 0 or time.monotonic() >= deadline or
                not any(busy in output for busy in DOLT_BUSY)):
            break
        time.sleep(random.uniform(delay, 2 * delay))
        delay = min(delay * 2, 2.0)
    if check and proc.returncode != 0:
        raise Fail(f"dolt {' '.join(args)} failed ({proc.returncode}): "
                   f"{(proc.stderr or proc.stdout).strip()[-2000:]}")
    return proc


def dolt_json(db: Path, query: str) -> list[dict[str, Any]]:
    out = dolt(["sql", "-r", "json", "-q", query], db).stdout.strip()
    return json.loads(out).get("rows", []) if out else []


IGNORED_CONFLICT_COLUMNS = {"updated_at", "content_hash", "last_activity", "started_at"}


def resolve_remote_wins(repo: Repo) -> list[dict[str, Any]]:
    """Merge the remote Beads branch, resolving every conflict to the remote side.

    Returns the local values that were dropped so the agent can reapply them.
    """
    if not shutil.which("dolt"):
        raise Fail("dolt CLI not found; it is needed to resolve Beads merge conflicts")
    db = dolt_db_dir(repo)
    branch = dolt_json(db, "select active_branch() as b")[0]["b"]
    dolt(["fetch", repo.config.remote], db)
    dolt(["merge", f"{repo.config.remote}/{branch}"], db, check=False)
    dropped: list[dict[str, Any]] = []
    for row in dolt_json(db, "select `table` as t from dolt_conflicts"):
        table = row["t"]
        for conflict in dolt_json(db, f"select * from `dolt_conflicts_{table}`"):
            ours = {k[4:]: v for k, v in conflict.items() if k.startswith("our_")}
            theirs = {k[6:]: v for k, v in conflict.items() if k.startswith("their_")}
            changed = {
                k: v
                for k, v in ours.items()
                if k not in IGNORED_CONFLICT_COLUMNS and theirs.get(k) != v
            }
            dropped.append(
                {"table": table, "id": ours.get("id") or theirs.get("id"), "dropped_local": changed}
            )
        dolt(["conflicts", "resolve", "--theirs", table], db)
    dolt(["add", "."], db)
    dolt(["commit", "--allow-empty", "-m", "enact sync: merge remote (remote wins)"], db, check=False)
    for item in dropped:
        log(f"conflict: remote won for {item['table']} {item['id']}; dropped {item['dropped_local']}")
    return dropped


# Serialize the read/check/write/sync sequence within one clone. Remote
# conflicts still resolve remote-wins; verify the published owner afterwards.
_ownership_locks: set[Path] = set()
_beads_reads: dict[Path, dict[tuple[str, ...], str]] = {}


OWNERSHIP_WAIT = 180
# After a fast check has passed, losing the checked work costs far more than waiting.
OWNERSHIP_WAIT_CHECKED = 3600


def lock_holder(path: Path) -> str:
    """Describe who holds the lock at `path` from its `.holder` record, for a waiter's log."""
    try:
        info = json.loads(path.with_name(path.name + ".holder").read_text())
        held = int(time.time() - info["since"])
        return f"pid {info['pid']} ({info['command']}), held for {held} s"
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown holder"


@contextlib.contextmanager
def ownership_lock(repo: Repo, wait: float = OWNERSHIP_WAIT):
    """Take the clone's ownership lock, waiting up to `wait` seconds.

    Callers that already passed a fast check use `OWNERSHIP_WAIT_CHECKED`. A holder record
    next to the lock names the process, so a waiter that gives up can say who kept it.
    """
    path = repo.state / "ownership.lock"
    if path in _ownership_locks:
        yield
        return
    start = time.monotonic()
    deadline = start + wait
    next_report = start + 60
    while (guard := try_lock(path)) is None:
        now = time.monotonic()
        if now >= deadline:
            holder = lock_holder(path)
            log(f"gave up waiting for the ownership lock after {int(now - start)} s; "
                f"held by {holder}")
            raise Fail(f"ownership operation busy (held by {holder}); retry", code=3)
        if now >= next_report:
            log(f"still waiting for the ownership lock, held by {lock_holder(path)}")
            next_report = now + 60
        time.sleep(0.1)
    holder_path = path.with_name(path.name + ".holder")
    try:
        write_json(holder_path, {"pid": os.getpid(), "command": " ".join(sys.argv[:4]),
                                 "since": time.time()})
    except OSError:
        pass
    _ownership_locks.add(path)
    _beads_reads[repo.common] = {}
    try:
        yield
    finally:
        _ownership_locks.remove(path)
        _beads_reads.pop(repo.common, None)
        holder_path.unlink(missing_ok=True)
        release_lock(path, guard)


def serialized(function):
    @functools.wraps(function)
    def call(first, *args, **kwargs):
        repo = first if isinstance(first, Repo) else find_repo()
        with ownership_lock(repo):
            return function(first, *args, **kwargs)
    return call


# Dolt appends every chunk a write makes to one journal and never compacts it
# on its own: a week of bd writes grew Beryl's to 410 MB, and every bd command,
# which opens the embedded store afresh, spent about a second reading it (0.2 s
# after a `dolt gc`). Sync compacts it once it passes this size.
DOLT_JOURNAL = "v" * 32
COMPACT_JOURNAL_BYTES = 64 << 20


@serialized
def sync(repo: Repo) -> dict[str, Any]:
    """Commit local Beads writes, merge the remote and push refs/dolt/data,
    then compact an embedded store whose journal has grown (`compact_beads`)."""
    result = sync_beads(repo)
    compact_beads(repo)
    return result


def compact_beads(repo: Repo) -> bool:
    """Run `dolt gc` on an embedded store whose journal passed
    COMPACT_JOURNAL_BYTES. It reclaims unreferenced chunks and rewrites the
    journal into table files without touching history, so the remote and other
    clones see nothing; `bd gc` would also squash commits they already have.
    Best effort: a failure is logged and sync's result stands."""
    try:
        db = dolt_db_dir(repo)
    except (Fail, OSError, KeyError, ValueError):
        return False  # server mode, or no Beads database here
    try:
        size = (db / ".dolt" / "noms" / DOLT_JOURNAL).stat().st_size
    except OSError:
        return False
    if size < COMPACT_JOURNAL_BYTES or not shutil.which("dolt"):
        return False
    proc = dolt(["gc"], db, check=False)
    if proc.returncode != 0:
        log(f"dolt gc failed: {(proc.stderr or proc.stdout).strip()[-500:]}")
        return False
    log(f"compacted the Beads store (journal was {size >> 20} MB)")
    return True


def sync_beads(repo: Repo) -> dict[str, Any]:
    _beads_reads.get(repo.common, {}).clear()
    beads = Beads(repo)
    # Embedded databases need no Beads schema/engine initialization to sync.
    # Keep the bd path for server mode and installations without the CLI.
    metadata = repo.main / ".beads" / "metadata.json"
    settings = json.loads(metadata.read_text(encoding="utf-8")) if metadata.exists() else {}
    embedded = (repo.config.remote == "origin"
                and settings.get("dolt_mode", "embedded") == "embedded"
                and settings.get("dolt_database") and shutil.which("dolt"))

    def command(*args: str) -> subprocess.CompletedProcess[str]:
        if not embedded:
            return beads.raw("dolt", *args)
        return dolt(list(args), dolt_db_dir(repo), check=False)

    if embedded:
        config = dict(line.split(" = ", 1) for line in
                      command("config", "--list").stdout.splitlines() if " = " in line)
        # bd supplies its own identity; the external CLI requires Dolt config.
        # Preserve that fallback rather than adding a new setup requirement.
        embedded = bool(config.get("user.name") and config.get("user.email"))
    remote = command("remote") if embedded else beads.raw("dolt", "remote", "list")
    if embedded and remote.returncode != 0:
        raise Fail(f"dolt remote failed: {(remote.stderr or remote.stdout).strip()}")
    if embedded and "origin" not in remote.stdout.split():
        embedded = False  # retain bd's remote selection for custom Dolt remotes
        remote = beads.raw("dolt", "remote", "list")
    if remote.returncode != 0 or not remote.stdout.strip():
        return {"remote": False, "dropped": []}
    branch = (dolt_json(dolt_db_dir(repo), "select active_branch() as b")[0]["b"]
              if embedded else None)
    target = [repo.config.remote, branch] if embedded else []
    dropped: list[dict[str, Any]] = []
    for attempt in range(8):
        committed = command("commit", *(["-A"] if embedded else []), "-m", "enact sync")
        if embedded and committed.returncode != 0:
            message = (committed.stdout + committed.stderr).lower()
            if "no changes added to commit" not in message and "nothing to commit" not in message:
                raise Fail(f"dolt commit failed: {(committed.stderr or committed.stdout).strip()}")
        pull = command("pull", *target)
        if pull.returncode != 0:
            text = (pull.stdout + pull.stderr).lower()
            if ("no branches found" in text or
                    (embedded and f'branch "{branch}" not found on remote'.lower() in text)):
                pass  # first push of this database to the remote
            elif "conflict" in text:
                dropped += resolve_remote_wins(repo)
            else:
                raise Fail(f"bd dolt pull failed: {(pull.stderr or pull.stdout).strip()}")
        if embedded:
            remote_ref = f"{repo.config.remote}/{branch}".replace("'", "''")
            try:
                heads = dolt_json(dolt_db_dir(repo),
                                  f"select hashof('HEAD') as local_tip, "
                                  f"hashof('{remote_ref}') as remote_tip")[0]
            except Fail:
                pass  # absent first-push ref or older CLI: use normal push
            else:
                if heads["local_tip"] == heads["remote_tip"]:
                    return {"remote": True, "dropped": dropped}
        push = command("push", *target)
        if push.returncode == 0:
            return {"remote": True, "dropped": dropped}
        log(f"beads push rejected (attempt {attempt + 1}); retrying")
        time.sleep(random.uniform(0.5, 2.0) * (attempt + 1))
    raise Fail("could not push Beads after repeated attempts")


# ---------------------------------------------------------------------------
# Claims and bead bookkeeping


def require_owner(bead: dict[str, Any], actor: str, *, closed: bool = False) -> None:
    actor = as_owner(actor, bead)
    if (bead.get("assignee") != actor or
            (bead.get("status") == "closed" and not closed)):
        raise Fail(f"{bead['id']} belongs to {bead.get('assignee') or 'nobody'} "
                   f"({bead.get('status')}); this session is {actor}", code=3)


def recovery_records(bead: dict[str, Any]) -> list[dict[str, Any]]:
    value = (bead.get("metadata") or {}).get("enact_recovery", [])
    return json.loads(value) if isinstance(value, str) else value


@serialized
def reassign(beads: Beads, bead_id: str, *fields: str, actor: str) -> None:
    """Hand over a bead whose owner Enact already fenced. bd 1.3 refuses to
    overwrite another actor's in_progress claim, and --force cannot be
    combined with --if-assignee; callers re-read the owner after syncing."""
    try:
        beads.run("update", bead_id, *fields, actor=actor)
    except Fail as error:
        if "cannot reassign" not in str(error):
            raise
        beads.run("update", bead_id, *fields, "--force", actor=actor)


def claim(repo: Repo, bead_id: str, actor: str, extra_meta: dict[str, str]) -> bool:
    """Claim a bead fleet-wide. Returns False when another actor won."""
    beads = Beads(repo)
    sync(repo)
    bead = beads.show(bead_id)
    require_running(bead, beads.all(), extra_meta.get("enact_run"))
    if bead.get("status") == "closed":
        return False
    if bead.get("assignee"):
        return bead["assignee"] == actor and all(
            meta(bead, key) == value for key, value in extra_meta.items())
    args = ["update", bead_id, "--claim"]
    for key, value in extra_meta.items():
        args += ["--set-metadata", f"{key}={value}"]
    if meta(bead, "enact_unowned"):
        args += ["--unset-metadata", "enact_unowned"]  # someone is repairing it now
    beads.run(*args, actor=actor)
    sync(repo)
    saved = beads.show(bead_id)
    require_running(saved, beads.all())
    return saved.get("assignee") == actor and all(
        meta(saved, key) == value for key, value in extra_meta.items())


def cmd_claim(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    actor = require_actor(args.actor)
    if not claim(repo, args.bead, actor, {}):
        raise Fail(f"{args.bead} is claimed by someone else or closed", code=3)
    return {"claimed": args.bead, "assignee": actor}


@serialized
def cmd_start(args: argparse.Namespace) -> dict[str, Any]:
    """Take only the reservation handed to this spawned session."""
    repo = find_repo()
    actor = require_actor(args.actor)
    beads = Beads(repo)
    sync(repo)
    bead = beads.show(args.bead)
    require_running(bead, beads.all())
    actor = as_owner(actor, bead)
    owner = bead.get("assignee") or ""
    token = meta(bead, "enact_reservation")
    # A pool hand-out names its member before the vizier records the binding.
    assigned = meta(bead, "enact_session") or meta(bead, "enact_pool_session")
    if bead.get("status") == "closed":
        raise Fail(f"{args.bead} is already closed", code=3)
    if owner != actor:
        if not owner.startswith("vizier:") or (assigned and assigned != actor):
            raise Fail(f"{args.bead} belongs to {owner}", code=3)
        # Legacy reservations have no token: only the recorded child may start.
        if (token and args.reservation != token) or (not token and assigned != actor):
            raise Fail("reservation does not match this worker", code=3)
    reassign(beads, args.bead, "--assignee", actor, "--status", "in_progress",
             "--set-metadata", f"enact_actor={actor}",
             "--set-metadata", f"enact_session={actor}", actor=actor)
    sync(repo)
    bead = beads.show(args.bead)
    require_owner(bead, actor)
    require_running(bead, beads.all())
    return {"bead": bead, "role": role_of(bead), "emoji": EMOJI.get(role_of(bead), "")}


@serialized
def cmd_close(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    actor = require_actor(args.actor)
    beads = Beads(repo)
    sync(repo)
    bead = beads.show(args.bead)
    require_owner(bead, actor, closed=True)
    if bead.get("issue_type") == "epic":
        require_running(bead, beads.all())
    # bd 1.3 only lets the assignee close, under the exact name it recorded.
    actor = as_owner(actor, bead)
    beads.run("close", args.bead, "--reason", args.reason, actor=actor)
    result = sync(repo)
    saved = beads.show(args.bead)
    require_owner(saved, actor, closed=True)
    if saved.get("status") != "closed":
        raise Fail("close lost during sync; recheck ownership", code=3)
    closed = close_finished_epics(repo)
    report = {"closed": args.bead, "epics_closed": closed, "sync": result}
    if args.continue_lane:
        # Under the same lock, so a vizier on this host cannot reserve it first.
        following = continue_lane(repo, beads, saved, actor)
        report["next"] = following and {key: following.get(key) for key in (
            "id", "title", "description", "acceptance_criteria", "labels")}
    return report


@serialized
def cmd_meta(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    actor = require_actor(args.actor)
    pairs = {}
    for item in args.pairs:
        key, sep, value = item.partition("=")
        if not sep:
            raise Fail(f"expected key=value, got {item!r}")
        pairs[key] = value
    sync(repo)
    beads = Beads(repo)
    bead = beads.show(args.bead)
    # Only the current vizier may bind a child/routing. An already started
    # child can be recorded idempotently, never replaced by a late spawn.
    routing = {"enact_session", "enact_spawn_app", "enact_spawn_project"}
    if set(pairs) <= routing and meta(bead, "enact_vizier") == actor:
        token = meta(bead, "enact_reservation")
        if token and args.reservation != token:
            raise Fail("reservation token missing or changed; check the original spawn action",
                       code=3)
        assigned = meta(bead, "enact_session")
        if assigned and pairs.get("enact_session", assigned) != assigned:
            raise Fail("worker already bound; release it before replacing", code=3)
    elif set(pairs) == {"enact_archived"} and (
            meta(bead, "enact_vizier") == actor or
            (args.expected_session == meta(bead, "enact_session") and
             args.expected_session and routed_here(bead))):
        if bead.get("status") != "closed":
            raise Fail("cannot archive an open assignment", code=3)
    else:
        if bead.get("issue_type") != "epic" or bead.get("assignee"):
            require_owner(bead, actor)
        protected = routing | {"enact_vizier", "enact_actor", "enact_reservation",
                               "enact_spawn_host", "enact_spawn_provider", "enact_recovery",
                               "enact_worktree", "enact_branch", "enact_worktree_host",
                               "enact_pool_session"}
        if protected.intersection(pairs):
            raise Fail("ownership and recovery fields are managed by Enact", code=3)
    if any(key.startswith(("enact_control", "enact_vizier_route_",
                           "enact_marshal_attempt_")) for key in pairs):  # legacy recovery keys
        raise Fail("run state and recovery fields are managed by enact run", code=3)
    beads.set_meta(args.bead, pairs, actor=actor)
    result = sync(repo)
    saved = beads.show(args.bead)
    if any(meta(saved, key) != value for key, value in pairs.items()):
        raise Fail("metadata lost during sync; recheck ownership", code=3)
    return {"bead": args.bead, "metadata": pairs, "sync": result}


def cmd_sync(args: argparse.Namespace) -> dict[str, Any]:
    return sync(find_repo())


def handoff(comments: list[dict[str, Any]]) -> str | None:
    """A finished bead's note for its successors: the latest comment starting
    with "Handoff", else its latest comment."""
    texts = [c.get("text") or "" for c in comments]
    notes = [t for t in texts if t.lower().startswith("handoff")]
    return (notes or texts or [None])[-1]


def cmd_brief(args: argparse.Namespace) -> dict[str, Any]:
    """Everything a session needs to start on a bead, in one compact read,
    instead of several verbose `bd show` calls."""
    repo = find_repo()
    beads = Beads(repo)
    bead = beads.show(args.bead)
    graph = beads.all()
    by_id = {b["id"]: b for b in graph}

    def comments(bead_id: str) -> list[dict[str, Any]]:
        return beads.json("comments", bead_id) or []

    def files(bead: dict[str, Any]) -> list[str]:
        found: list[str] = []
        for sha in (meta(bead, "enact_commits") or "").split():
            out = repo.git("diff-tree", "--no-commit-id", "--name-only", "-r", sha, check=False)
            found += [f for f in out.splitlines() if f and f not in found]
        return found

    epics = []
    parent = bead.get("parent")
    while parent in by_id and len(epics) < 10:
        epic = by_id[parent]
        epics.append({k: epic.get(k) for k in ("id", "title", "description",
                                               "acceptance_criteria", "labels")})
        parent = epic.get("parent")
    dependencies = []
    for dep in bead.get("dependencies") or []:
        if dep.get("dependency_type") == "parent-child":
            continue
        full = by_id.get(dep["id"], dep)
        dependencies.append({
            "id": dep["id"], "title": full.get("title"), "status": full.get("status"),
            "role": role_of(full), "close_reason": full.get("close_reason"),
            "commits": (meta(full, "enact_commits") or "").split(),
            "files": files(full), "handoff": handoff(comments(dep["id"])),
        })
    return {
        "bead": {**{k: bead.get(k) for k in (
            "id", "title", "status", "priority", "assignee", "labels", "description",
            "acceptance_criteria", "notes", "design")},
            "commits": (meta(bead, "enact_commits") or "").split(),
            "comments": [c.get("text") for c in comments(args.bead)]},
        "epics": epics,
        "dependencies": dependencies,
        "lane": [{"id": b["id"], "title": b.get("title"), "status": b.get("status"),
                  "assignee": b.get("assignee")}
                 for b in sorted(graph, key=spawn_order) if same_lane(bead, b, graph)],
    }


# ---------------------------------------------------------------------------
# Vizier scheduling


def descendants(graph: list[dict[str, Any]], root: str) -> list[dict[str, Any]]:
    children: dict[str, list[dict[str, Any]]] = {}
    for bead in graph:
        if bead.get("parent"):
            children.setdefault(bead["parent"], []).append(bead)
    found: list[dict[str, Any]] = []
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child["id"])
    return found


def archivist_waits(run: list[dict[str, Any]]) -> bool:
    """The Archivist documents the shipped state, so it starts only once it is
    the last open work in the run, including follow-ups filed mid-run."""
    return any(
        b.get("status") != "closed" and b.get("issue_type") != "epic" and role_of(b) != "archivist"
        for b in run
    )


def bead_runs(bead: dict[str, Any], graph: list[dict[str, Any]]) -> list[str]:
    """Explicit run links also cover unparented Stewards; infer old runs."""
    runs = (meta(bead, "enact_runs") or "").split()
    if meta(bead, "enact_run"):
        runs.append(meta(bead, "enact_run"))
    by_id = {b["id"]: b for b in graph}
    current = bead
    seen = set()
    while current.get("parent") in by_id and current["id"] not in seen:
        seen.add(current["id"])
        current = by_id[current["parent"]]
    if current.get("issue_type") == "epic":
        runs.append(current["id"])
    return list(dict.fromkeys(runs))


# A lane continuation copies these so the vizier and archive
# bookkeeping see the next bead as the same session's work.
LANE_CARRY = ("enact_run", "enact_vizier", "enact_spawn_host", "enact_spawn_provider",
              "enact_spawn_app", "enact_spawn_project", "enact_session", "enact_actor",
              "enact_pool_session")


def same_lane(bead: dict[str, Any], other: dict[str, Any], graph: list[dict[str, Any]]) -> bool:
    """Another bead with the same `lane:<name>` label in the same run."""
    lane = label_value(bead, "lane")
    return (bool(lane) and other["id"] != bead["id"] and label_value(other, "lane") == lane
            and bool(set(bead_runs(bead, graph)) & set(bead_runs(other, graph))))


def lane_held(bead: dict[str, Any], graph: list[dict[str, Any]]) -> bool:
    """One session works a lane at a time, so its warm context carries over."""
    return any(same_lane(bead, other, graph) and other.get("status") != "closed"
               and other.get("assignee") for other in graph)


def busy_sessions(graph: list[dict[str, Any]]) -> set[str]:
    """Sessions still holding an open bead: never archive or reuse them."""
    return {meta(b, key) or "" for b in graph for key in ("enact_session", "enact_pool_session")
            if b.get("status") != "closed" and b.get("assignee")} - {""}


# ---------------------------------------------------------------------------
# Session pool (Claude Desktop)
#
# Claude Desktop lets one session start at most POOL_LIMIT sessions over its
# whole life, and only in a turn the user started. A vizier therefore starts
# a fixed pool in its first turn and hands every bead to an idle member with
# clear_session and send_message, which work from any turn. The pool is
# machine-local state per vizier; which members are busy comes from the graph.

POOL_LIMIT = 10
# A member that refused clear_session (still finishing, open on screen) is
# skipped for this long before it is offered again.
POOL_REST_SECONDS = 60


def pool_path(repo: Repo, vizier: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", vizier)
    return repo.state / f"pool-{safe}.json"


def load_pool(repo: Repo, vizier: str) -> dict[str, Any]:
    try:
        pool = json.loads(pool_path(repo, vizier).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pool = {}
    return {"sessions": list(pool.get("sessions", [])), "started": int(pool.get("started", 0)),
            "resting": dict(pool.get("resting", {}))}


def save_pool(repo: Repo, vizier: str, pool: dict[str, Any]) -> None:
    write_json(pool_path(repo, vizier), pool)


def idle_members(pool: dict[str, Any], graph: list[dict[str, Any]],
                 justiciar: dict[str, Any] | None) -> list[str]:
    """Members holding no open bead, no Justiciar duty and no rest, in turn."""
    busy = busy_sessions(graph)
    if justiciar and justiciar.get("pool_session"):
        busy.add(justiciar["pool_session"])
    clock = time.time()
    return [s for s in pool["sessions"]
            if s not in busy and pool["resting"].get(s, 0) <= clock]


def take_member(repo: Repo, vizier: str, pool: dict[str, Any], session: str) -> None:
    """Rotate a handed-out member to the back so work spreads over the pool."""
    pool["sessions"] = [s for s in pool["sessions"] if s != session] + [session]
    pool["resting"].pop(session, None)
    save_pool(repo, vizier, pool)


def cmd_pool(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    vizier = args.vizier or require_actor(None)
    pool = load_pool(repo, vizier)
    sessions = args.sessions or []
    if args.action == "add":
        new = [s for s in dict.fromkeys(sessions) if s not in pool["sessions"]]
        if pool["started"] + len(new) > POOL_LIMIT:
            raise Fail(f"a vizier may start at most {POOL_LIMIT} sessions; "
                       f"{pool['started']} already started", code=3, pool=pool)
        pool["sessions"] += new
        pool["started"] += len(new)
    elif args.action == "drop":
        gone = set(pool["sessions"]) if args.all else set(sessions)
        pool["sessions"] = [s for s in pool["sessions"] if s not in gone]
        for session in gone:
            pool["resting"].pop(session, None)
    elif args.action == "rest":
        until = time.time() + POOL_REST_SECONDS
        for session in sessions:
            if session in pool["sessions"]:
                pool["resting"][session] = until
    if args.action != "status":
        save_pool(repo, vizier, pool)
    graph = Beads(repo).all()
    return {"vizier": vizier, "pool": pool["sessions"], "started": pool["started"],
            "limit": POOL_LIMIT, "idle": idle_members(pool, graph, justiciar_state(repo))}


def continue_lane(repo: Repo, beads: Beads, closed: dict[str, Any],
                  actor: str) -> dict[str, Any] | None:
    """Claim the next ready bead of the closed bead's lane for the same session."""
    if not label_value(closed, "lane"):
        return None
    graph = beads.all()
    by_id = {b["id"]: b for b in graph}
    ready = beads.json("ready", "-n", "0", "--exclude-type", "epic", "--unassigned") or []
    options = [by_id.get(b["id"], b) for b in ready]
    options = sorted((b for b in options
                      if same_lane(closed, b, graph) and role_of(b) == role_of(closed)
                      and targeted_here(b) and not stopped_runs(b, graph)), key=spawn_order)
    carry = {key: meta(closed, key) or "" for key in LANE_CARRY if meta(closed, key)}
    if meta(closed, "enact_reservation"):
        carry["enact_reservation"] = uuid.uuid4().hex
    for bead in options:
        if claim(repo, bead["id"], actor, carry):
            return beads.show(bead["id"])
    return None


def run_delivery(repo: Repo, graph: list[dict[str, Any]], epic: str,
                 ancestry: dict[str, set[str]] | None = None) -> dict[str, Any]:
    # Status checks every epic. Cache reachability by tip across those runs;
    # one git process per commit/range made busy projects take minutes.
    ancestry = ancestry if ancestry is not None else {}

    def is_ancestor(sha: str, tip: str) -> bool:
        if tip not in ancestry:
            ancestry[tip] = set(repo.git("rev-list", tip, check=False).split())
        return sha in ancestry[tip]

    scope = {epic, *(b["id"] for b in descendants(graph, epic))}
    members = [b for b in graph if b["id"] in scope or epic in bead_runs(b, graph)]
    # Failure ranges also recover links when delivery metadata arrived after
    # CI filed the bead. Explicit run links need not be exhaustive.
    while True:
        commits = list(dict.fromkeys(
            sha for b in members for sha in (meta(b, "enact_commits") or "").split()
        ))
        legacy = []
        for bead in graph:
            if bead in members or role_of(bead) != "steward":
                continue
            green, sep, tip = (meta(bead, "enact_ci_range") or "").partition("..")
            if sep and any(is_ancestor(sha, tip)
                           and not is_ancestor(sha, green)
                           for sha in commits):
                legacy.append(bead)
        if not legacy:
            break
        members += legacy
    master = repo.remote_ref(repo.config.master)
    pending = [sha for sha in commits if not is_ancestor(sha, master)]
    stewards = [b["id"] for b in members
                if role_of(b) == "steward" and b.get("status") != "closed"]
    return {"commits": commits, "pending": pending, "stewards": stewards,
            "complete": not pending and not stewards}


# Shared run control is separate from Beads issue status: paused workers may
# finish, but no new assignment or automatic recovery may start.
def json_meta(bead: dict[str, Any], key: str, default: Any) -> Any:
    value = (bead.get("metadata") or {}).get(key, default)
    return json.loads(value) if isinstance(value, str) else value


def run_control(bead: dict[str, Any]) -> dict[str, Any]:
    return json_meta(bead, "enact_control", {"state": "active", "epoch": "legacy"})


def run_finished(bead: dict[str, Any]) -> bool:
    """The vizier declared the run over with `enact run finish`."""
    return run_control(bead)["state"] == "finished"


def work_done(bead: dict[str, Any]) -> bool:
    """A deferred bead was set aside on purpose; it no longer holds a run
    or its epic open."""
    return bead.get("status") in ("closed", "deferred")


def stopped_runs(bead: dict[str, Any], graph: list[dict[str, Any]],
                 extra: str | None = None) -> list[dict[str, Any]]:
    by_id = {b["id"]: b for b in graph}
    ids = set(bead_runs(bead, graph)) | {bead["id"]}
    if extra:
        ids.add(extra)
    # A milestone may be paused independently of its top-level run.
    for start in list(ids):
        while start in by_id:
            parent = by_id[start].get("parent")
            if not parent or parent in ids:
                break
            ids.add(parent)
            start = parent
    return [by_id[i] for i in ids if i in by_id and
            run_control(by_id[i])["state"] in ("paused", "blocked")]


def require_running(bead: dict[str, Any], graph: list[dict[str, Any]],
                    extra: str | None = None) -> None:
    if halted := stopped_runs(bead, graph, extra):
        raise Fail("run is paused or blocked; only explicit resume permits new work",
                   code=3, runs={b["id"]: run_control(b) for b in halted})


def save_run_meta(repo: Repo, epic: str, key: str, value: Any) -> None:
    beads = Beads(repo)
    beads.set_meta(epic, {key: json.dumps(value)})
    sync(repo)
    if json_meta(beads.show(epic), key, None) != value:
        raise Fail("run update lost during sync; inspect shared state", code=3)


def route_key(session: str) -> str:
    return "enact_vizier_route_" + hashlib.sha256(session.encode()).hexdigest()[:24]


def run_routes(epic: dict[str, Any]) -> list[dict[str, Any]]:
    return [json_meta(epic, key, {}) for key in (epic.get("metadata") or {})
            if key.startswith("enact_vizier_route_")]


def lease_path(repo: Repo, vizier: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", vizier)
    return repo.state / f"vizier-{safe}.json"


def renew_lease(repo: Repo, vizier: str, epic: str) -> None:
    """A vizier's heartbeat. A route on the epic says a vizier once ran the
    run; only a fresh lease says one is still looping and will repair this
    machine's slow-CI failures. Leases are local files because each machine
    repairs only its own failures."""
    write_json(lease_path(repo, vizier), {"session": vizier, "epic": epic,
                                          "host": host_name(), "at": time.time()})


def drop_lease(repo: Repo, vizier: str) -> None:
    lease_path(repo, vizier).unlink(missing_ok=True)


def lease_age(repo: Repo, vizier: str, epic: str | None) -> float | None:
    """Seconds since the vizier last renewed its lease on this epic (on any
    epic when `epic` is None), or None when it holds none here (another epic,
    or a vizier older than leases)."""
    try:
        lease = json.loads(lease_path(repo, vizier).read_text(encoding="utf-8"))
        if epic is not None and lease.get("epic") != epic:
            return None
        return max(time.time() - float(lease["at"]), 0.0)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def lease_fresh(repo: Repo, vizier: str, epic: str | None) -> bool:
    age = lease_age(repo, vizier, epic)
    return age is not None and age < VIZIER_LEASE_SECONDS


def route_alive(repo: Repo, epic: str, route: dict[str, Any]) -> bool:
    """The route's vizier runs on this machine and renewed its lease recently."""
    return (route.get("host") == host_name() and bool(route.get("session"))
            and lease_fresh(repo, route["session"], epic))


def run_live(repo: Repo, epic: dict[str, Any], graph: list[dict[str, Any]]) -> bool:
    """A vizier on this machine is looping on this run, which is not held."""
    return not stopped_runs(epic, graph) and not run_finished(epic) and any(
        route_alive(repo, epic["id"], route) for route in run_routes(epic))


def runs_watched(repo: Repo, graph: list[dict[str, Any]], runs: list[str]) -> bool:
    """A live vizier here will repair a slow-CI failure in these runs' commits."""
    by_id = {b["id"]: b for b in graph}
    return any(run in by_id and run_live(repo, by_id[run], graph) for run in runs)


def route_leases(repo: Repo, epic: str,
                 routes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Each route with its vizier's lease age in whole seconds (None when it
    holds no lease on this epic here), and whether the run is stale: no route
    has a fresh lease, so no vizier is looping on it."""
    aged = []
    for route in routes:
        age = lease_age(repo, route["session"], epic) if route.get("session") else None
        aged.append({**route, "lease_age": None if age is None else int(age)})
    return aged, not any(route_alive(repo, epic, route) for route in routes)


def seconds_since(stamp: str | None) -> float | None:
    try:
        then = dt.datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.timezone.utc)
    return max((dt.datetime.now(dt.timezone.utc) - then).total_seconds(), 0.0)


def unowned_stewards(repo: Repo, graph: list[dict[str, Any]],
                     deliveries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Open Steward beads for this machine's slow-CI failures that nobody is
    repairing. A live vizier here covers a Steward it would schedule: one
    without a parent, or one in its run's delivery. Otherwise an assignee
    counts as owner while its own lease is fresh or it touched the bead
    within a lease's lifetime; an assignee that did neither is presumed gone."""
    live = [b for b in graph if b.get("issue_type") == "epic" and run_live(repo, b, graph)]
    unowned = []
    for bead in graph:
        if (role_of(bead) != "steward" or work_done(bead)
                or not steward_targeted_here(bead)):
            continue
        if any(not bead.get("parent") or bead["id"] in deliveries.get(epic["id"], {}).get(
                "stewards", ()) for epic in live):
            continue
        assignee = bead.get("assignee") or None
        idle = seconds_since(bead.get("updated_at"))
        if assignee and (lease_fresh(repo, assignee, None)
                         or (idle is not None and idle < VIZIER_LEASE_SECONDS)):
            continue
        unowned.append({"id": bead["id"], "title": bead.get("title"), "assignee": assignee,
                        "updated_at": bead.get("updated_at"),
                        "range": meta(bead, "enact_ci_range")})
    return unowned


def bead_watched(repo: Repo, graph: list[dict[str, Any]], bead: dict[str, Any]) -> bool:
    """Fresh evidence that a vizier will repair slow CI for this bead's push:
    the vizier that spawned it still renews its lease on that run, or some
    run the bead belongs to is live. Without that evidence the pushing
    session watches slow CI itself: a needless watch costs it some waiting,
    while a missing one left Beryl's master stuck for hours."""
    vizier, run = meta(bead, "enact_vizier"), meta(bead, "enact_run")
    if vizier and run and lease_fresh(repo, vizier, run):
        return True
    return bool(bead) and runs_watched(repo, graph, bead_runs(bead, graph))


def register_run(repo: Repo, epic: str, vizier: str, parallel: int,
                 authorized: bool = False) -> dict[str, Any]:
    bead = Beads(repo).show(epic)
    if bead.get("issue_type") != "epic":
        raise Fail("run must name an epic")
    key = route_key(vizier)
    old = json_meta(bead, key, {})
    route = {"session": vizier, "host": host_name(), "provider": provider(),
             "app": os.environ.get("ENACT_APP", provider()), "project": str(repo.main),
             "parallel": parallel, "authorized": authorized or old.get("authorized", False)}
    if old != route:
        save_run_meta(repo, epic, key, route)
    return route


@serialized
def close_finished_epics(repo: Repo) -> list[str]:
    # bd close-eligible has no exclusion for paused/blocked runs. Close only
    # eligible active epics, bottom-up, preserving held acceptance criteria.
    beads = Beads(repo)
    graph = beads.all()
    closed = []
    while True:
        eligible = [b for b in graph if b.get("issue_type") == "epic"
                    and b.get("status") != "closed" and not stopped_runs(b, graph)
                    and (children := [c for c in graph if c.get("parent") == b["id"]])
                    and all(work_done(c) for c in children)]
        if not eligible:
            if closed:
                sync(repo)
            return closed
        for bead in eligible:
            # bd refuses to close an epic with deferred children unless forced;
            # the filter above already proved every child is closed or deferred.
            try:
                beads.run("close", bead["id"], "--reason", "All children complete", "--force")
            except Fail as failure:
                # One stubborn epic must not abort every caller (`enact next`).
                log(f"could not close {bead['id']}: {failure}")
                graph = [b for b in graph if b["id"] != bead["id"]]
                continue
            bead["status"] = "closed"
            closed.append(bead["id"])


def stale_runs(repo: Repo, graph: list[dict[str, Any]],
               deliveries: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Unfinished runs whose vizier on this machine stopped renewing its lease:
    every route is on this host and none has a fresh lease. A run with a
    route on another host is left alone, since that host's leases cannot be
    read here. Each entry has the epic bead and its delivery."""
    stale = []
    for bead in graph:
        if bead.get("issue_type") != "epic" or run_finished(bead) or stopped_runs(bead, graph):
            continue
        routes = vizier_routes(bead, graph)
        if not routes or any(r.get("host") != host_name() for r in routes):
            continue
        delivery = (deliveries or {}).get(bead["id"]) or run_delivery(repo, graph, bead["id"])
        if not any(route_alive(repo, bead["id"], r) for r in routes):
            stale.append({"bead": bead, "delivery": delivery})
    return stale


@serialized
def prune_runs(repo: Repo) -> list[dict[str, Any]]:
    """Retire the runs `stale_runs` finds: a fully delivered run is marked
    `finished`; one with work pending is `paused`, naming the expired lease,
    so a returning vizier resumes it with `enact run resume --explicit`."""
    sync(repo)
    graph = Beads(repo).all()
    pruned = []
    for item in stale_runs(repo, graph):
        bead, delivery = item["bead"], item["delivery"]
        complete = delivery["complete"]
        control = run_control(bead)
        reason = ("pruned: delivery complete and its vizier's lease expired" if complete
                  else "pruned: its vizier's lease expired with work pending")
        state = "finished" if complete else "paused"
        save_run_meta(repo, bead["id"], "enact_control",
                      {"state": state, "epoch": control["epoch"], "reason": reason,
                       "actor": "enact run prune", "at": now()})
        if state == "paused" and bead.get("status") == "closed":
            Beads(repo).run("reopen", bead["id"], "--reason", reason)
            sync(repo)
        pruned.append({"epic": bead["id"], "state": state, "reason": reason})
    return pruned


@serialized
def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    if args.action == "prune":
        return {"pruned": prune_runs(repo)}
    if not args.epic:
        raise Fail(f"run {args.action} needs an epic")
    sync(repo)
    beads = Beads(repo)
    bead = beads.show(args.epic)
    if bead.get("issue_type") != "epic":
        raise Fail("run must name an epic")
    actor = require_actor(None)
    control = run_control(bead)
    if args.action == "register":
        return {"epic": args.epic, "control": control,
                "route": register_run(repo, args.epic, actor, args.parallel, args.authorized)}
    if args.action == "finish":
        if not args.reason:
            raise Fail("run transitions require --reason")
        if control["state"] in ("paused", "blocked"):
            raise Fail("a paused or blocked run needs an explicit resume before it can finish",
                       code=3)
        control = {"state": "finished", "epoch": control["epoch"],
                   "reason": args.reason, "actor": actor, "at": now()}
        save_run_meta(repo, args.epic, "enact_control", control)
        return {"epic": args.epic, "control": control}
    if args.action in ("pause", "block", "resume"):
        if not args.reason:
            raise Fail("run transitions require --reason")
        if args.action == "resume" and not args.explicit:
            raise Fail("resume requires --explicit for a user's explicit instruction", code=3)
        if args.action == "block" and not args.exhausted:
            raise Fail("block requires --exhausted after Justiciar recovery", code=3)
        control = {"state": {"pause": "paused", "block": "blocked", "resume": "active"}[args.action],
                   "epoch": uuid.uuid4().hex if args.action == "resume" else control["epoch"],
                   "reason": args.reason, "actor": actor, "at": now()}
        save_run_meta(repo, args.epic, "enact_control", control)
        if args.action != "resume" and bead.get("status") == "closed":
            beads.run("reopen", args.epic, "--reason", args.reason)
            sync(repo)
        return {"epic": args.epic, "control": control}
    raise Fail(f"unknown run action {args.action}")


def spawn_prompt(role: str, bead: dict[str, Any], epic: str, vizier: str) -> str:
    script = Path(__file__).resolve()
    python = sys.executable or "python3"
    token = meta(bead, "enact_reservation")
    start = f"enact start {bead['id']}" + (f" --reservation {token}" if token else "")
    return (
        f"You are an Enact {role} session (emoji {EMOJI.get(role, '')}). "
        f"Your bead is {bead['id']} ({bead.get('title', '')}) in epic {epic}; "
        f"you were started by vizier {vizier} on host {host_name()}.\n\n"
        f"Read {ROLES_DIR / 'common.md'} and then {ROLES_DIR / (role + '.md')}, "
        f"and follow them. The Enact helper is `\"{python}\" \"{script}\"`, "
        f"referred to as `enact` in those files.\n"
        f"Start with `{start}`. Retitle ✅ (or ⛔) before your final turn ends."
    )


def session_title(role: str, bead: dict[str, Any]) -> str:
    title = bead.get("title", "")
    if len(title) > 48:
        title = title[:47].rstrip() + "…"
    return f"{EMOJI.get(role, '')} [{bead['id']}] {title}"


def spawn_action(repo: Repo, bead: dict[str, Any], epic: str, vizier: str) -> dict[str, Any]:
    role = role_of(bead)
    if role not in SPAWNED_ROLES:
        role = "executor"
    host_provider = provider()
    settings = repo.config.role_settings(role, host_provider, label_value(bead, "tier"))
    claude_model = settings.get("model") if host_provider == "claude" else None
    action = {
        "action": "spawn",
        "bead": bead["id"],
        "role": role,
        "reservation": meta(bead, "enact_reservation"),
        "title": session_title(role, bead),
        "prompt": spawn_prompt(role, bead, epic, vizier),
        "settings": settings,
    }
    if claude_model:
        action["model_required"] = MODEL_REQUIRED
    return action


# Restated on Claude spawns that name a model, so it survives the vizier's
# context compaction (the 2026-11-10 tier:fable bead ran on Opus).
MODEL_REQUIRED = (
    "settings.model is required, never a hint. Claude Desktop: start_session "
    "silently ignores a model pricier than yours, so start the child with the "
    "holding prompt, set_session_model it to settings.model, confirm with "
    "get_session, then send `prompt` with send_message (enact-plan skill, "
    "spawn step). If it cannot be switched, stop the child and release."
)


# Restated on every pool hand-out, so it survives the vizier's compaction.
POOL_NOTICE = (
    "Pool hand-out: never call start_session for this. One call at a time, never "
    "batched: clear_session `session`; only once the clear succeeds, "
    "set_session_model (and set_session_effort) to settings, or to your own model "
    "and effort when settings names none, confirming a required model with "
    "get_session; send `prompt` with send_message; then record `enact meta <bead> "
    "enact_session=<session> --reservation <token>` (no bead for a Justiciar). If "
    "clear or send is refused because the session is busy, pinned or open on "
    "screen, run `enact pool rest <session>` and leave its model alone (a model "
    "change before a clear blocks clearing until the session runs a turn; an "
    "idle session refused for live work recovers after you send it 'Reply only "
    "`ok` and end your turn.'); if it is archived or missing, "
    "`enact pool drop <session>`. Then release the reservation: `enact release "
    "<bead> --reservation <token> --reason \"<refusal>\"` (for a Justiciar, "
    "`enact justiciar break` and request it again)."
)


POOL_REMINDER = (
    "Pool vizier: act on this result (pool hand-outs follow `pool_notice`; archive "
    "listed sessions only when the action is done), then start `enact next` again "
    "with the same arguments as a background command and end your turn. Its exit "
    "wakes you. Never run two at once, and never start sessions after pre-flight: "
    "only a user-started turn may, and each vizier may start ten in its life."
)


LOOP_REMINDER = (
    "Vizier loop: act on this result (archive listed sessions and record "
    "enact_archived=true; start one session per spawn action and record its "
    "id with `enact meta <bead> enact_session=<id> --reservation <token>` "
    "using that spawn action's reservation), then run `enact next` "
    "again with the same arguments. Never end your turn before action: done, paused or blocked, "
    "including when the epic is closed but delivery or Steward repairs are pending. "
    "Run `enact next` in the foreground and continue in this turn."
)

CODEX_LOOP_REMINDER = (
    "Vizier loop: handle this action, including archive and spawn bookkeeping. "
    "For action: wait, use event-driven `wait_threads` on active "
    "`inflight_threads` for up to five minutes, then recheck with "
    "`enact next --wait 0`. If none can signal progress, use the foreground "
    "`enact next --wait 540` fallback. Keep one scheduler process at a time "
    "and continue until done, paused or blocked."
)


@serialized
def schedule(repo: Repo, epic: str, parallel: int, vizier: str) -> dict[str, Any]:
    fetch(repo)
    recover_deliveries(repo)
    beads = Beads(repo)
    register_run(repo, epic, vizier, parallel)
    close_finished_epics(repo)
    graph = beads.all()
    by_id = {b["id"]: b for b in graph}
    if epic not in by_id:
        raise Fail(f"epic {epic} not found")
    mine = [b for b in graph if meta(b, "enact_vizier") == vizier]
    busy = busy_sessions(graph)
    pool = load_pool(repo, vizier) if provider() == "claude" else {}
    members = set(pool.get("sessions", []))
    # Once a vizier has a pool it never starts sessions again, even if every
    # member is lost: it may be running in a turn that cannot.
    pooled = pool.get("started", 0) > 0
    finished = [
        {"bead": b["id"], "session": meta(b, "enact_session")}
        for b in mine
        if b.get("status") == "closed"
        and meta(b, "enact_session")
        and meta(b, "enact_session") not in busy
        and meta(b, "enact_archived") != "true"
    ]
    result: dict[str, Any] = {
        "epic": epic,
        "vizier": vizier,
        "host": host_name(),
        # Pool members are reused, so they are archived only when the run ends.
        "archive": [item for item in finished if item["session"] not in members],
        "reminder": (POOL_REMINDER if pooled else CODEX_LOOP_REMINDER
                     if provider() == "codex" else LOOP_REMINDER),
    }
    if pooled:
        result["pool_notice"] = POOL_NOTICE
    inflight = [b for b in mine if b.get("status") != "closed"]
    result["inflight"] = [b["id"] for b in inflight]
    if provider() == "codex":
        # The host can wait for these chats without another Beads lookup.
        result["inflight_threads"] = sorted({meta(b, "enact_session") for b in inflight
                                              if meta(b, "enact_session")})

    delivery = run_delivery(repo, graph, epic)
    result["delivery"] = delivery
    if halted := stopped_runs(by_id[epic], graph):
        control = run_control(halted[0])
        return {**result, "action": control["state"], "control": control}

    if run_finished(by_id[epic]):
        control = run_control(by_id[epic])
        return {**result, "action": "finished", "control": control}

    request = justiciar_state(repo)
    idle = idle_members(pool, graph, request) if pooled else []
    if request and request.get("state") == "requested" and request.get("host") == host_name():
        result["justiciar"] = request
        member = idle[0] if idle else None
        changes = {"state": "spawning", "vizier": vizier}
        if member:
            changes["pool_session"] = member
        if (member or not pooled) and justiciar_transition(repo, request, changes):
            action = {
                **result,
                "action": "spawn-justiciar",
                "title": f"{EMOJI['justiciar']} {request.get('reason', 'Emergency')[:48]}",
                "prompt": justiciar_prompt(request),
                "settings": repo.config.role_settings("justiciar", provider()),
            }
            if member:
                take_member(repo, vizier, pool, member)
                action["session"] = member
            return action

    if by_id[epic].get("status") == "closed" and not inflight and delivery["complete"]:
        # The run is over: archive the whole pool with the other finished chats.
        if pooled:
            listed = {item["session"] for item in finished}
            result["pool_archive"] = sorted(members - busy - listed)
        return {**result, "action": "done", "archive": finished}
    if len(inflight) >= parallel:
        return {**result, "action": "wait", "reason": "capacity"}

    scope = {b["id"] for b in descendants(graph, epic)} | set(delivery["stewards"])
    hold_archivist = archivist_waits([by_id[i] for i in scope])
    ready = beads.json("ready", "-n", "0", "--exclude-type", "epic", "--unassigned") or []
    candidates = [
        b
        for b in ready
        if (b["id"] in scope or (role_of(b) == "steward" and not b.get("parent")))
        and targeted_here(b)
        and not stopped_runs(b, graph, epic)
        and not (hold_archivist and role_of(b) == "archivist")
        and not lane_held(by_id.get(b["id"], b), graph)
    ]
    candidates.sort(key=spawn_order)
    if candidates and pooled and not members:
        # Only a turn the user starts may start replacements.
        return {**result, "action": "pool-empty", "started": pool["started"],
                "limit": POOL_LIMIT}
    if candidates and pooled and not idle:
        return {**result, "action": "wait", "reason": "pool busy"}
    for bead in candidates:
        reservation = {
            "enact_run": epic,
            "enact_vizier": vizier,
            "enact_spawn_host": host_name(),
            "enact_spawn_provider": provider(),
            "enact_spawn_app": os.environ.get("ENACT_APP", provider()),
            "enact_spawn_project": str(repo.main),
            "enact_reservation": uuid.uuid4().hex,
        }
        if pooled:
            reservation["enact_pool_session"] = idle[0]
        if claim(repo, bead["id"], f"vizier:{host_name()}:{provider()}:{vizier}", reservation):
            action = spawn_action(repo, beads.show(bead["id"]), epic, vizier)
            if pooled:
                take_member(repo, vizier, pool, idle[0])
                # A pool member already exists: no holding prompt is needed.
                action.pop("model_required", None)
                action["session"] = idle[0]
            return {**result, **action}
        log(f"lost the claim race for {bead['id']}; trying the next bead")
    if candidates:
        # The graph predates the winning claim; it cannot prove a deadlock.
        return {**result, "action": "wait", "reason": "claims changed"}

    open_scope = [by_id[i] for i in scope if not work_done(by_id[i])]
    working = [b for b in open_scope if b.get("status") == "in_progress"]
    elsewhere = [b for b in ready if b["id"] in scope and not targeted_here(b)]
    if not working and not inflight:
        held = [h for b in open_scope for h in stopped_runs(b, graph)]
        if held:
            control = run_control(held[0])
            return {**result, "action": control["state"], "control": control}
    # A pin nobody serves cannot make progress: the Justiciar unpins it or
    # moves it out of the epic rather than letting the run wait on it.
    stuck = [b["id"] for b in elsewhere if stranded(repo, b, by_id[epic])]
    if open_scope and not working and len(stuck) == len(elsewhere) and not inflight:
        action = {**result, "action": "deadlock", "open": [b["id"] for b in open_scope]}
        return {**action, "stranded": stuck} if stuck else action
    stale = stale_hours(open_scope)
    if open_scope and not inflight and stale is not None and stale >= 6:
        return {**result, "action": "deadlock", "reason": f"no change for {stale:.1f}h"}
    if delivery["pending"]:
        if repo.config.slow:
            result["ci"] = start_ci(repo)
        return {**result, "action": "wait", "reason": "delivery pending"}
    return {**result, "action": "wait", "reason": "nothing ready"}


def stale_hours(beads_list: list[dict[str, Any]]) -> float | None:
    stamps = []
    for bead in beads_list:
        try:
            stamps.append(dt.datetime.fromisoformat(bead["updated_at"].replace("Z", "+00:00")))
        except (KeyError, ValueError):
            continue
    if not stamps:
        return None
    return (dt.datetime.now(dt.timezone.utc) - max(stamps)).total_seconds() / 3600


def cmd_next(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    vizier = args.vizier or require_actor(None)
    # A background wait must never overlap another for the same vizier: both
    # could hand out the same pool member.
    lock = repo.state / f"next-{pool_path(repo, vizier).stem}.lock"
    guard = try_lock(lock)
    if guard is None:
        raise Fail("another `enact next` is already running for this vizier; "
                   "wait for it to exit", code=3)
    try:
        return next_step(repo, args, vizier)
    finally:
        release_lock(lock, guard)


def next_step(repo: Repo, args: argparse.Namespace, vizier: str) -> dict[str, Any]:
    deadline = time.monotonic() + max(args.wait, 0)
    # Archives already reported (and possibly refused because the session was
    # still busy) only wake the vizier again at the end of the wait.
    reported_file = repo.state / f"archive-reported-{vizier.replace(':', '_')}.json"
    try:
        reported = set(json.loads(reported_file.read_text()))
    except (OSError, ValueError):
        reported = set()
    while True:
        renew_lease(repo, vizier, args.epic)
        sync(repo)
        result = schedule(repo, args.epic, args.parallel, vizier)
        pending = {item["bead"] for item in result["archive"]}
        timed_out = time.monotonic() >= deadline
        if result["action"] in ("done", "paused", "blocked", "finished"):
            # The vizier stops looping, so nothing here repairs slow CI for it.
            drop_lease(repo, vizier)
        if result["action"] != "wait" or pending - reported or timed_out:
            reported_file.write_text(json.dumps(sorted(pending)))
            return result
        time.sleep(min(args.interval, max(deadline - time.monotonic(), 1)))


def cmd_wait(args: argparse.Namespace) -> dict[str, Any]:
    """Block until every named bead is closed, for work that must start after."""
    repo = find_repo()
    deadline = time.monotonic() + max(args.timeout, 0)
    while True:
        sync(repo)
        beads = Beads(repo)
        open_beads = [{"id": b["id"], "status": b.get("status"), "assignee": b.get("assignee")}
                      for b in map(beads.show, args.beads) if b.get("status") != "closed"]
        if not open_beads:
            return {"state": "closed", "beads": args.beads}
        if time.monotonic() >= deadline:
            return {"state": "waiting", "open": open_beads}
        time.sleep(min(args.interval, max(deadline - time.monotonic(), 1)))

# ---------------------------------------------------------------------------
# Justiciar lock (one ref per machine on the remote, independent of Beads)


def justiciar_ref(host: str | None = None) -> str:
    return JUSTICIAR_PREFIX + (host or host_name())


def justiciar_state(repo: Repo, host: str | None = None) -> dict[str, Any] | None:
    """This machine's lock, or the legacy fleet-wide lock when this machine holds it."""
    host = host or host_name()
    remote, ref = repo.config.remote, justiciar_ref(host)
    listing = repo.git("ls-remote", remote, ref, LEGACY_JUSTICIAR_REF, check=False)
    found = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2:
            found[parts[1]] = parts[0]
    for candidate in (ref, LEGACY_JUSTICIAR_REF):
        sha = found.get(candidate)
        if not sha:
            continue
        repo.git("fetch", "-q", remote, f"+{candidate}:{candidate}", check=False)
        body = repo.git("log", "-1", "--format=%B", sha, check=False)
        try:
            state = json.loads(body)
        except json.JSONDecodeError:
            state = {"state": "unknown", "raw": body}
        if candidate == LEGACY_JUSTICIAR_REF and state.get("host") != host:
            continue  # another machine's Justiciar no longer blocks this one
        state["sha"], state["ref"] = sha, candidate
        return state
    return None


def justiciar_commit(repo: Repo, payload: dict[str, Any]) -> str:
    proc = subprocess.run(
        ["git", "mktree"], cwd=repo.top, input="", text=True, capture_output=True, check=True
    )
    empty_tree = proc.stdout.strip()
    message = json.dumps(payload, sort_keys=True)
    return repo.git("commit-tree", empty_tree, "-m", message)


def justiciar_push(repo: Repo, sha: str, expect: str, ref: str | None = None) -> bool:
    ref = ref or justiciar_ref()
    lease = f"--force-with-lease={ref}:{expect}"
    target = f"{sha}:{ref}" if sha else f":{ref}"
    return repo.git_ok("push", "-q", lease, repo.config.remote, target)


def justiciar_transition(repo: Repo, current: dict[str, Any], changes: dict[str, Any]) -> bool:
    payload = {k: v for k, v in current.items() if k not in ("sha", "ref")}
    payload.update(changes, updated=now())
    return justiciar_push(repo, justiciar_commit(repo, payload), current["sha"],
                          current.get("ref"))


def justiciar_prompt(request: dict[str, Any]) -> str:
    script = Path(__file__).resolve()
    python = sys.executable or "python3"
    return (
        f"You are the Enact justiciar session (emoji {EMOJI['justiciar']}), requested by "
        f"{request.get('requester', 'unknown')} on host {request.get('host')}: "
        f"{request.get('reason', '')}\n\n"
        f"Read {ROLES_DIR / 'common.md'} and then {ROLES_DIR / 'justiciar.md'}, and follow "
        f"them. The Enact helper is `\"{python}\" \"{script}\"`, referred to as `enact`."
    )


def cmd_justiciar(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    current = justiciar_state(repo)
    if args.action == "status":
        return {"justiciar": current}
    if args.action == "request":
        if current:
            raise Fail("a justiciar is already requested or active on this machine", code=3, holder=current)
        payload = {
            "state": "requested",
            "host": host_name(),
            "provider": provider(),
            "requester": args.actor or session_actor() or "unknown",
            "reason": args.reason or "",
            "created": now(),
        }
        if not justiciar_push(repo, justiciar_commit(repo, payload), ""):
            raise Fail("lost the race: another justiciar request exists", code=3,
                       holder=justiciar_state(repo))
        return {"requested": payload}
    if args.action == "acquire":
        actor = require_actor(args.actor)
        if current and current.get("state") == "active" and current.get("session") != actor:
            raise Fail("another justiciar is active on this machine", code=3, holder=current)
        if current:
            ok = justiciar_transition(repo, current, {"state": "active", "session": actor})
        else:
            payload = {"state": "active", "host": host_name(), "provider": provider(),
                       "session": actor, "requester": actor, "reason": args.reason or "",
                       "created": now()}
            ok = justiciar_push(repo, justiciar_commit(repo, payload), "")
        if not ok:
            raise Fail("lost the race for the justiciar lock", code=3, holder=justiciar_state(repo))
        return {"acquired": justiciar_state(repo)}
    if args.action in ("release", "break"):
        if not current:
            return {"released": None}
        actor = args.actor or session_actor()
        if args.action == "release" and current.get("session") not in (None, actor):
            raise Fail("the lock belongs to another session; use `break`", code=3, holder=current)
        if not justiciar_push(repo, "", current["sha"], current.get("ref")):
            raise Fail("lock changed while releasing", code=3, holder=justiciar_state(repo))
        return {"released": current}
    raise Fail(f"unknown action {args.action}")


# ---------------------------------------------------------------------------
# Local branches that track the remote

TRACKED_OK = ("current", "fast-forwarded", "created")


def fetch(repo: Repo, check: bool = True) -> dict[str, str]:
    # Embedded Dolt updates refs/remotes/<remote>/__dolt_remote_info__ in this
    # same repository, so a concurrent sync can make git lose a ref lock.
    for attempt in range(5):
        proc = run(["git", "fetch", "-q", repo.config.remote], repo.main, check=False)
        if proc.returncode == 0 or "cannot lock ref" not in proc.stderr or attempt == 4:
            break
        time.sleep(0.2 * (attempt + 1) + random.random() * 0.2)
    if check and proc.returncode != 0:
        raise Fail(f"git fetch -q {repo.config.remote} failed ({proc.returncode}): "
                   f"{(proc.stderr or proc.stdout).strip()[-2000:]}")
    return track_local_branches(repo)


def worktree_entries(repo: Repo) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain`: one dict per worktree, keyed
    by attribute (`worktree`, `HEAD`, `branch`, `detached`, `locked`, ...)."""
    entries: list[dict[str, str]] = []
    for line in repo.git("worktree", "list", "--porcelain", cwd=repo.main).splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            entries.append({})
        if entries and key:
            entries[-1][key] = value
    return entries


def checked_out_branches(repo: Repo) -> dict[str, Path]:
    """Map each branch ref checked out in a worktree to that worktree."""
    return {entry["branch"]: Path(entry["worktree"])
            for entry in worktree_entries(repo) if "branch" in entry}


def track_branch(repo: Repo, branch: str, checkout: Path | None) -> str:
    remote_ref = repo.remote_ref(branch)
    remote = repo.git("rev-parse", "-q", "--verify", remote_ref, cwd=repo.main, check=False)
    if not remote:
        return "no remote branch"
    ref = f"refs/heads/{branch}"
    local = repo.git("rev-parse", "-q", "--verify", ref, cwd=repo.main, check=False)
    if not local:
        ok = repo.git_ok("branch", "-q", "--track", branch, remote_ref, cwd=repo.main)
        return "created" if ok else "could not create"
    if local == remote:
        return "current"
    if not repo.git_ok("merge-base", "--is-ancestor", local, remote, cwd=repo.main):
        return "diverged"
    if checkout is None:
        ok = repo.git_ok("update-ref", ref, remote, local, cwd=repo.main)
        return "fast-forwarded" if ok else "moved concurrently"
    if repo.git("status", "--porcelain", "--untracked-files=no", cwd=checkout, check=False):
        return f"checkout {checkout} has local changes"
    ok = repo.git_ok("merge", "--ff-only", "-q", remote, cwd=checkout)
    return "fast-forwarded" if ok else f"fast-forward failed in {checkout}"


def track_local_branches(repo: Repo) -> dict[str, str]:
    """Fast-forward local staging and master to their remote branches.

    Best effort: a diverged branch or a dirty checkout is left alone and
    reported, never fixed.
    """
    checkouts = checked_out_branches(repo)
    result = {}
    for branch in dict.fromkeys((repo.config.staging, repo.config.master)):
        state = track_branch(repo, branch, checkouts.get(f"refs/heads/{branch}"))
        if state not in TRACKED_OK:
            log(f"local {branch} not updated: {state}")
        result[branch] = state
    return result


# ---------------------------------------------------------------------------
# Worktrees


def worktree_env(repo: Repo, path: Path, branch: str, base: str) -> dict[str, str]:
    return {
        "ENACT_WORKTREE_PATH": str(path),
        "ENACT_BRANCH": branch,
        "ENACT_BASE_SHA": base,
        "ENACT_REPO": str(repo.main),
    }


def create_worktree(repo: Repo, name: str, branch: str | None, base: str) -> Path:
    path = repo.worktree_root() / name
    if path.exists():
        raise Fail(f"worktree path {path} already exists")
    # `enact setup` puts the root in .gitignore; this covers clones without it.
    repo.ignore_worktree_root(repo.common / "info" / "exclude")
    path.parent.mkdir(parents=True, exist_ok=True)
    if repo.config.worktree_create:
        env = worktree_env(repo, path, branch or "", base)
        if run_shell(repo.config.worktree_create, repo.main, env) != 0:
            raise Fail("worktree create hook failed")
    elif branch:
        if repo.git_ok("rev-parse", "--verify", f"refs/heads/{branch}"):
            # A missing checkout does not imply that its branch was delivered.
            # Git still refuses a branch checked out in a live or locked tree.
            repo.git("worktree", "prune", cwd=repo.main)
            repo.git("worktree", "add", "-q", str(path), branch, cwd=repo.main)
        else:
            repo.git("worktree", "add", "-q", "-b", branch, str(path), base, cwd=repo.main)
    else:
        repo.git("worktree", "add", "-q", "--detach", str(path), base, cwd=repo.main)
    return path


def is_slot(repo: Repo, path: Path) -> bool:
    """Whether `path` is one of the fixed slot directories that bead worktrees reuse."""
    return (repo.config.worktree_slots > 0 and re.fullmatch(r"slot-\d+", path.name) is not None
            and path.resolve().parent == repo.worktree_root().resolve())


def claim_slot(repo: Repo, branch: str, base: str) -> Path | None:
    """Check `branch` out in a free slot, or return None when every slot is busy.

    A cargo build's fingerprints include the worktree's path, so a bead that starts in a
    directory built before skips recompiling the workspace crates (measured on Beryl:
    115 s instead of 224 s for a build plus test build). A free slot is a registered worktree
    on a detached HEAD. Its ignored files (`rust/target`, `node_modules`) are kept; tracked
    changes and untracked files are discarded, since a released slot holds no work.
    """
    parked = {Path(entry["worktree"]).resolve() for entry in worktree_entries(repo)
              if "detached" in entry}
    for number in range(1, repo.config.worktree_slots + 1):
        path = repo.worktree_root() / f"slot-{number:02d}"
        if path.resolve() in parked:
            repo.git("reset", "-q", "--hard", cwd=path)
            repo.git("clean", "-q", "-fd", cwd=path)
        elif not path.exists():
            create_worktree(repo, path.name, None, base)
        else:
            continue
        if repo.git_ok("rev-parse", "--verify", f"refs/heads/{branch}"):
            repo.git("checkout", "-q", branch, cwd=path)
        else:
            repo.git("checkout", "-q", "-b", branch, base, cwd=path)
        return path
    return None


def release_slot(repo: Repo, path: Path, branch: str | None) -> bool:
    """Return a slot to the pool: detach it at staging and drop its branch, keeping the
    build directories. False means git refused, and the caller deletes the tree instead."""
    staging = repo.remote_ref(repo.config.staging)
    for step in (("reset", "-q", "--hard"), ("clean", "-q", "-fd"),
                 ("checkout", "-q", "--detach", staging)):
        done = run(["git", *step], path, check=False)
        if done.returncode != 0:
            log(f"slot {path} not released ({' '.join(step)}): {done.stderr.strip()}")
            return False
    if branch:
        repo.git("branch", "-D", branch, cwd=repo.main, check=False)
    return True


def owns_slot(bead: dict[str, Any], path: Path, branch: str) -> bool:
    """Whether `bead` is the one working in slot `path` now. The bead of a previous
    occupant still records the same path, so the slot's branch decides."""
    return meta(bead, "enact_worktree") == str(path) and meta(bead, "enact_branch") == branch


def process_ancestors() -> set[int]:
    """This process and every process above it, which reaping must spare."""
    spared = {os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in spared:
        spared.add(pid)
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                             text=True, check=False).stdout.strip()
        pid = int(out) if out.isdigit() else 0
    return spared


def processes_inside(path: Path) -> dict[int, str]:
    """Map pid to command for processes whose cwd or executable is under `path`.

    Uses `lsof` for the working directory and executable of every process
    (macOS has no /proc); where lsof is missing, reads /proc instead.
    """
    root = str(path.resolve())
    found: dict[int, str] = {}

    def inside(candidate: str) -> bool:
        return candidate == root or candidate.startswith(root + os.sep)

    if shutil.which("lsof"):
        lister = subprocess.Popen(["lsof", "-w", "-F", "pcn", "-d", "cwd,txt"],
                                  stdout=subprocess.PIPE, text=True)
        out = lister.communicate()[0]
        pid, command = 0, ""
        for line in out.splitlines():
            if line[:1] == "p" and line[1:].isdigit():
                pid, command = int(line[1:]), ""
            elif line[:1] == "c":
                command = line[1:]
            elif line[:1] == "n" and inside(line[1:]):
                found[pid] = command
        found.pop(lister.pid, None)  # the lister inherits this process's cwd
    elif Path("/proc/self").exists():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            for link in ("cwd", "exe"):
                with contextlib.suppress(OSError):
                    if inside(os.readlink(entry / link)):
                        found[int(entry.name)] = (entry / "comm").read_text().strip()
    return found


def reap_processes(path: Path, grace: float = 3.0) -> list[dict[str, Any]]:
    """End every process using a worktree: SIGTERM, then SIGKILL after `grace` seconds.

    A fuzz run started in a bead's worktree once kept a core busy for hours
    after the bead closed and the tree was deleted. Never ends this process
    or its ancestors, whose working directory may be the tree itself, nor
    anything in this process's group: `enact push --bead X | jq` runs the
    reader in the same group, and ending it breaks the pipe the result is
    written to. A leftover background run from the bead has its own group.
    """
    if WINDOWS:
        log(f"not ending processes in {path}: unsupported on Windows")
        return []
    spared = process_ancestors()
    own_group = os.getpgid(0)
    victims = {pid: command for pid, command in processes_inside(path).items()
               if pid not in spared and process_group(pid) != own_group}
    for pid in victims:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    alive = set(victims)
    while alive and time.monotonic() < deadline:
        alive = {pid for pid in alive if not process_exited(pid)}
        if alive:
            time.sleep(0.1)
    for pid in alive:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    for pid, command in victims.items():
        log(f"ended process {pid} ({command}) using worktree {path}")
    return [{"pid": pid, "command": command} for pid, command in victims.items()]


def process_group(pid: int) -> int | None:
    """The process group of `pid`, or None when it has already exited."""
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def process_exited(pid: int) -> bool:
    # A terminated child of this process stays a zombie until it is reaped.
    with contextlib.suppress(ChildProcessError):
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return True
    return not pid_alive(pid)


def remove_worktree(repo: Repo, path: Path, branch: str | None) -> list[dict[str, Any]]:
    """Delete a worktree and its branch; return the processes ended inside it."""
    ended = reap_processes(path) if path.exists() else []
    if path.exists() and is_slot(repo, path) and release_slot(repo, path, branch):
        return ended
    if repo.config.worktree_remove:
        env = worktree_env(repo, path, branch or "", "")
        if run_shell(repo.config.worktree_remove, repo.main, env) != 0:
            raise Fail("worktree remove hook failed")
        return ended
    repo.git("worktree", "remove", "--force", str(path), cwd=repo.main, check=False)
    # A build server still writing output can refill a tree as git deletes
    # it. Git no longer tracks what is left, so finish the job if possible;
    # the sweep retries anything still in use.
    if path.exists() and not any(Path(entry["worktree"]).resolve() == path.resolve()
                                 for entry in worktree_entries(repo)):
        with contextlib.suppress(OSError):
            rmtree(path)
    if branch:
        repo.git("branch", "-D", branch, cwd=repo.main, check=False)
    repo.git("worktree", "prune", cwd=repo.main, check=False)
    return ended


def rmtree(path: Path) -> None:
    """shutil.rmtree that also deletes read-only files (git's objects on Windows)."""
    def writable(func: Any, target: str, _: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def sweep_worktrees(repo: Repo, ci_trees: bool) -> dict[str, Any]:
    """Delete worktrees nothing can still need; report what stays and why.

    Bead trees go once their bead is closed and they hold no uncommitted,
    untracked or unpublished work. Slow-CI trees go only for the caller
    holding the CI lock, under which none can be live. Directories git no
    longer tracks go after an hour untouched. Other branches, locked bead
    trees, separate repositories and loose files are never touched.
    """
    root = repo.worktree_root().resolve()
    if not root.is_dir():
        return {"removed": [], "kept": {}}
    repo.git("worktree", "prune", cwd=repo.main, check=False)
    graph: list[dict[str, Any]] | None = None  # only closed beads release trees
    if (repo.main / ".beads").is_dir():
        with contextlib.suppress(Fail, OSError, ValueError):
            graph = Beads(repo).all()
    staging = repo.remote_ref(repo.config.staging)
    removed: list[str] = []
    kept: dict[str, str] = {}
    ended: list[dict[str, Any]] = []
    registered = set()

    def remove(path: Path, branch: str | None) -> None:
        try:
            ended.extend(remove_worktree(repo, path, branch))
        except (Fail, OSError) as error:
            kept[str(path)] = f"removal failed: {error}"
            return
        log(f"swept worktree {path}")
        removed.append(str(path))

    for entry in worktree_entries(repo):
        path = Path(entry["worktree"])
        registered.add(path.resolve())
        if path.resolve().parent != root or not path.is_dir():
            continue
        branch = entry.get("branch", "").removeprefix("refs/heads/")
        if path.resolve() == repo.top.resolve():
            kept[str(path)] = "current checkout"
        elif path.name.startswith("ci-") and "detached" in entry:
            if not ci_trees:
                kept[str(path)] = "the slow-CI runner sweeps its own trees"
                continue
            # A runner killed inside `git worktree add` leaves git's lock.
            repo.git("worktree", "unlock", str(path), cwd=repo.main, check=False)
            remove(path, None)
        elif is_slot(repo, path) and "detached" in entry:
            kept[str(path)] = "idle slot"
        elif not branch.startswith("enact/"):
            kept[str(path)] = "not an Enact branch"
        elif "locked" in entry:
            kept[str(path)] = "locked"
        elif not (bead := next((b for b in graph or [] if b["id"] == path.name or
                                (owns_slot(b, path, branch) if is_slot(repo, path)
                                 else meta(b, "enact_worktree") == str(path))), None)):
            kept[str(path)] = "no bead" if graph is not None else "Beads unavailable"
        elif bead.get("status") != "closed":
            kept[str(path)] = f"bead {bead['id']} is {bead.get('status')}"
        elif repo.git("status", "--porcelain", cwd=path, check=False):
            kept[str(path)] = "uncommitted or untracked files"
        elif not repo.git_ok("merge-base", "--is-ancestor", "HEAD", staging, cwd=path):
            kept[str(path)] = f"commits not on {staging}"
        else:
            remove(path, branch)
    # Hooks must still leave registered git worktrees (push and recovery run
    # git in them), so anything else here is a leftover, unless it holds
    # trees (a hook's pool) or is hidden (a hook's cache, say).
    for path in sorted(root.iterdir()):
        if path.resolve() in registered or not path.is_dir() or path.is_symlink():
            continue
        if path.name.startswith(".") or any(
                tree.is_relative_to(path.resolve()) for tree in registered):
            kept[str(path)] = "hidden or holds worktrees"
        elif (path / ".git").exists():
            # Another repository, or a checkout that lost its registration
            # and may still hold edits git can no longer report.
            kept[str(path)] = "has a .git this repository does not track; inspect by hand"
        elif time.time() - path.stat().st_mtime < SWEEP_AGE:
            kept[str(path)] = "changed within the last hour"
        else:
            ended.extend(reap_processes(path))
            try:
                rmtree(path)
            except OSError as error:
                kept[str(path)] = f"removal failed: {error}"
                continue
            log(f"swept stray directory {path}")
            removed.append(str(path))
    return {"removed": removed, "kept": kept, **({"ended_processes": ended} if ended else {})}


def worktree_bead(repo: Repo, path: Path) -> dict[str, Any] | None:
    # Includes legacy worktrees named after a bead, created before pointers
    # were recorded. Never let omitting --bead bypass ownership checks.
    slot = is_slot(repo, path)
    slot_branch = repo.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path, check=False) \
        if slot and path.is_dir() else ""
    for bead in Beads(repo).all():
        if path == repo.worktree_root() / bead["id"] or (
                meta(bead, "enact_worktree_host") == host_name() and
                (owns_slot(bead, path, slot_branch) if slot
                 else meta(bead, "enact_worktree") == str(path))) or any(
                    r.get("host") == host_name() and r.get("path") == str(path)
                    for r in recovery_records(bead)):
            return bead
    return None


def verified_worktree(repo: Repo, path: Path, branch: str) -> None:
    if (not path.is_dir() or find_repo(path).common != repo.common or
            repo.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path) != branch):
        raise Fail(f"recovery worktree does not match recorded repository/branch: {path}", code=2)


def cmd_worktree(args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "prune":
        repo = find_repo()
        fetch(repo, check=False)
        # Slow-CI trees belong to the runner, which sweeps them under its lock.
        return sweep_worktrees(repo, ci_trees=False)
    if not args.name:
        raise Fail(f"enact worktree {args.action} needs a name")
    return bead_worktree(args)


@serialized
def bead_worktree(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    path = repo.worktree_root() / args.name
    branch = f"enact/{args.name}"
    sync(repo)
    bead = worktree_bead(repo, path)
    actor = require_actor(None) if bead else None
    if bead:
        # A bead working in a slot has no directory named after it.
        recorded = Path(meta(bead, "enact_worktree") or "/")
        if (meta(bead, "enact_worktree_host") == host_name() and is_slot(repo, recorded)
                and recorded.is_dir() and owns_slot(bead, recorded, branch)):
            path = recorded
        # Retain direct CLI use for never-assigned beads by claiming first.
        if (not bead.get("assignee") and not recovery_records(bead) and
                not meta(bead, "enact_reservation") and args.action == "create"):
            if not claim(repo, bead["id"], actor, {}):
                raise Fail("worktree claim lost", code=3)
            bead = Beads(repo).show(bead["id"])
        require_owner(bead, actor)
    if args.action == "create":
        fetch(repo)
        base = repo.git("rev-parse", repo.remote_ref(repo.config.staging), cwd=repo.main)
        reused = False
        if bead:
            records = [r for r in recovery_records(bead)
                       if r.get("host") == host_name() and r.get("path")]
            if records:
                record = records[-1]
                old = Path(record["path"])
                if old.exists():
                    if not record.get("inactive"):
                        raise Fail("prior worker inactivity is unconfirmed", code=3)
                    path, branch = old, record["branch"]
                    verified_worktree(repo, path, branch)
                    reused = True
            # Idempotent retries after a lost response, never a blind reset.
            if not reused and path.exists() and meta(bead, "enact_worktree") == str(path):
                if meta(bead, "enact_worktree_host") == host_name():
                    verified_worktree(repo, path, branch)
                    reused = True
        if not reused:
            saved_head = repo.git("rev-parse", "--verify", f"refs/heads/{branch}",
                                  check=False)
            if saved_head:
                base, reused = saved_head, True
            slot = claim_slot(repo, branch, base) if bead else None
            path = slot or create_worktree(repo, args.name, branch, base)
        if bead:
            Beads(repo).set_meta(bead["id"], {
                "enact_worktree": str(path), "enact_branch": branch,
                "enact_worktree_host": host_name()}, actor=actor)
            sync(repo)
            require_owner(Beads(repo).show(bead["id"]), actor)
        result = {"path": str(path), "branch": branch, "base": base, "recovered": reused}
        # Run sessions title from their role files; ad hoc ones learn it here.
        return result if bead else {**result, "title": TITLE_NOTICE}
    # Explicit removal must not discard unfinished work, including untracked
    # files. Delivery's own cleanup follows a confirmed push instead.
    fetch(repo)
    if path.exists():
        verified_worktree(repo, path, branch)
        if repo.git("status", "--porcelain", cwd=path):
            raise Fail("worktree contains unfinished work; preserve it for recovery", code=2)
    if (repo.git_ok("rev-parse", "--verify", f"refs/heads/{branch}") and
            not repo.git_ok("merge-base", "--is-ancestor", branch,
                            repo.remote_ref(repo.config.staging))):
        raise Fail("branch contains unpublished commits; preserve it for recovery", code=2)
    ended = remove_worktree(repo, path, branch)
    return {"removed": str(path), **({"ended_processes": ended} if ended else {})}


# ---------------------------------------------------------------------------
# Promotion to staging


def integrate(repo: Repo) -> None:
    """Rebase onto the remote staging tip, then fold in a diverged master."""
    staging = repo.remote_ref(repo.config.staging)
    master = repo.remote_ref(repo.config.master)
    fetch(repo)
    if not repo.git_ok("rebase", "-q", staging):
        raise Fail(
            f"rebase onto {staging} hit conflicts: resolve them, run "
            "`git rebase --continue`, then run `enact push` again",
            code=2,
        )
    has_master = repo.git_ok("rev-parse", "-q", "--verify", master)
    if has_master and not repo.git_ok("merge-base", "--is-ancestor", master, "HEAD"):
        log(f"{master} has commits that are not on staging; merging them in")
        if not repo.git_ok("merge", "--no-edit", "-m", f"Merge {master} into staging", master):
            raise Fail(
                f"merging {master} hit conflicts: resolve them, commit, then run "
                "`enact push` again",
                code=2,
            )


def delivery_receipts(repo: Repo) -> Path:
    path = repo.state / "deliveries"
    path.mkdir(exist_ok=True)
    return path


@serialized
def finish_delivery(repo: Repo, receipt: Path, data: dict[str, Any],
                    start: bool = True) -> dict[str, Any]:
    """Replay bookkeeping after a confirmed push; retain receipt on any failure.

    Without `start`, a delivery that wants slow CI keeps its receipt: the
    caller starts CI and retires it (`cmd_push`, after removing its worktree).
    """
    result: dict[str, Any] = {"staging": data["head"], "commits": data["commits"]}
    if not receipt.exists():
        # Pushes no longer hold the lock while they check, so two local
        # sessions can both find one receipt; the first to lock it finishes it.
        return result
    if data.get("bead"):
        beads = Beads(repo)
        # Remote-wins merges can discard a metadata write. Re-read and union,
        # then verify the published value before retiring the durable receipt.
        for _ in range(3):
            bead = beads.show(data["bead"])
            pairs = {}
            for key, values in (("enact_commits", data["history"]),
                                ("enact_runs", data["runs"])):
                pairs[key] = " ".join(dict.fromkeys(values + (meta(bead, key) or "").split()))
            beads.set_meta(data["bead"], pairs, actor=session_actor())
            result["sync"] = sync(repo)
            saved = beads.show(data["bead"])
            if all(set(value.split()) <= set((meta(saved, key) or "").split())
                   for key, value in pairs.items()):
                break
        else:
            raise Fail("delivery metadata lost during sync; retry enact push", code=2)
    if data["ci"] and not start:
        return result
    if data["ci"]:
        result["ci"] = start_ci(repo)
    receipt.unlink(missing_ok=True)
    return result


def recover_deliveries(repo: Repo) -> list[dict[str, Any]]:
    """Only replay receipts whose push reached staging or green master."""
    recovered = []
    for receipt in sorted(delivery_receipts(repo).glob("*.json")):
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # another local session finished this receipt
        if any(repo.git_ok("merge-base", "--is-ancestor", data["head"], repo.remote_ref(branch))
               for branch in (repo.config.staging, repo.config.master)):
            recovered.append(finish_delivery(repo, receipt, data))
    return recovered


# ---------------------------------------------------------------------------
# Merge queue
#
# With `[push] queue = true`, `enact push` rebases in its own tree as before, then adds its
# branch to the host's queue (files in `<git common dir>/enact/queue`) and waits for a
# verdict. Whichever waiting push holds `queue.lock` is the integrator: it rebases a batch of
# queued branches in order onto staging in a tree of its own, runs the fast check once on the
# result and pushes it. There is no daemon: when the integrator exits or dies, the OS frees
# the lock and the next waiting push takes over the entries still without a verdict.

QUEUE_POLL_SECONDS = 1.0
# Hidden, so no bead's worktree can take the name.
QUEUE_TREE = ".merge-queue"


def queue_dir(repo: Repo) -> Path:
    path = repo.state / "queue"
    path.mkdir(exist_ok=True)
    return path


def verdict_path(repo: Repo, entry_id: str) -> Path:
    return queue_dir(repo) / f"{entry_id}.verdict.json"


def drop_entry(repo: Repo, entry_id: str) -> None:
    (queue_dir(repo) / f"{entry_id}.json").unlink(missing_ok=True)
    verdict_path(repo, entry_id).unlink(missing_ok=True)


def queued(repo: Repo) -> list[dict[str, Any]]:
    """Entries still waiting for a verdict, oldest first. An entry whose push process has
    died is dropped: nobody is left to finish its delivery or to fix a failure."""
    entries = []
    for path in queue_dir(repo).glob("*.json"):
        if path.name.endswith(".verdict.json"):
            # A verdict for an entry dropped while its batch ran has no reader.
            if not path.with_name(path.name.removesuffix(".verdict.json") + ".json").exists():
                path.unlink(missing_ok=True)
            continue
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            continue
        if not pid_alive(entry["pid"]):
            drop_entry(repo, entry["id"])
        elif not verdict_path(repo, entry["id"]).exists():
            entries.append(entry)
    return sorted(entries, key=lambda e: (e["enqueued"], e["id"]))


def decide(repo: Repo, entry: dict[str, Any], verdict: dict[str, Any]) -> None:
    write_json(verdict_path(repo, entry["id"]), verdict)


def set_aside(entries: list[dict[str, Any]], output: str) -> list[dict[str, Any]]:
    """The entries to check apart from the rest after their batch failed its fast check:
    those whose changed files the check's output names, or, when it names none or all of
    them, the later half."""
    named = [e for e in entries if any(name in output for name in e["files"])]
    if 0 < len(named) < len(entries):
        return named
    return entries[len(entries) // 2:]


def queue_tree(repo: Repo) -> Path:
    """The integrator's worktree. It is kept between batches, on a detached HEAD, so its
    build directories stay warm; the sweep keeps it as a tree not on an Enact branch."""
    path = repo.worktree_root() / QUEUE_TREE
    registered = {Path(e["worktree"]).resolve() for e in worktree_entries(repo)}
    if path.resolve() not in registered:
        if path.exists():
            rmtree(path)  # a leftover git no longer tracks
        repo.git("worktree", "prune", cwd=repo.main)
        create_worktree(repo, QUEUE_TREE, None, repo.remote_ref(repo.config.staging))
    return path


def settle_landed(repo: Repo, entries: list[dict[str, Any]]) -> set[str]:
    """Give a landed verdict to each entry whose push reached staging under an integrator
    that died before recording the verdicts; its delivery receipt says so."""
    ids = {e["id"]: e for e in entries}
    settled = set()
    for receipt in delivery_receipts(repo).glob("*.json"):
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            continue
        entry = ids.get(data.get("queue", ""))
        if entry and repo.git_ok("merge-base", "--is-ancestor", data["head"],
                                 repo.remote_ref(repo.config.staging)):
            decide(repo, entry, {"status": "landed", "receipt": str(receipt), "delivery": data,
                                 "staging": data["head"]})
            settled.add(entry["id"])
    return settled


def run_queue(repo: Repo, own: str) -> None:
    """Land queued branches, a batch at a time, until entry `own` has a verdict. The caller
    holds `queue.lock`."""
    tree = queue_tree(repo)
    stats = {"checks": 0}
    while not verdict_path(repo, own).exists():
        fetch(repo)
        entries = queued(repo)
        if own not in {e["id"] for e in entries}:
            break  # replaced by a newer push from the same tree
        settled = settle_landed(repo, entries)
        batch = [e for e in entries if e["id"] not in settled][:repo.config.push_batch]
        if batch:
            land(repo, tree, batch, stats)


def land(repo: Repo, tree: Path, entries: list[dict[str, Any]], stats: dict[str, int]) -> None:
    """Rebase `entries` in order onto staging in `tree`, run the fast check once on the result
    and push it.

    An entry that no longer applies gets a `conflict` verdict and its push re-integrates in
    its own tree. A failing check splits the batch (`set_aside`): the rest land without the
    set-aside entries, which are then checked on their own. One bad branch among five whose
    files the check names costs three checks, and the other four land in one push.
    """
    cfg = repo.config
    staging = repo.remote_ref(cfg.staging)
    for attempt in range(6):
        entries = [e for e in entries if not verdict_path(repo, e["id"]).exists()]
        fetch(repo)
        repo.git("reset", "-q", "--hard", staging, cwd=tree)
        repo.git("clean", "-q", "-fd", cwd=tree)
        picked = []
        for entry in entries:
            before = repo.git("rev-parse", "HEAD", cwd=tree)
            if entry["base"] == before:  # keeps the branch's own commits
                applied = repo.git_ok("merge", "-q", "--ff-only", entry["head"], cwd=tree)
            else:
                applied = repo.git_ok("cherry-pick", "--allow-empty",
                                      f"{entry['base']}..{entry['head']}", cwd=tree)
            if not applied:
                repo.git_ok("cherry-pick", "--abort", cwd=tree)
                repo.git("reset", "-q", "--hard", before, cwd=tree)
                decide(repo, entry, {"status": "conflict"})
                continue
            picked.append({**entry, "head": repo.git("rev-parse", "HEAD", cwd=tree),
                           "commits": repo.git("rev-list", "--reverse", f"{before}..HEAD",
                                               cwd=tree).split()})
        if not picked:
            return
        fast = find_repo(tree).config.fast
        log(f"merge queue: checking {len(picked)} branch(es) on {staging}")
        code, output = run_bounded(fast, tree, cfg.push_timeout) if fast else (0, "")
        stats["checks"] += 1
        if code:
            if len(picked) == 1:
                decide(repo, picked[0], {"status": "failed", "output": output[-20000:]})
                return
            aside = {e["id"] for e in set_aside(picked, output)}
            land(repo, tree, [e for e in entries if e["id"] not in aside], stats)
            land(repo, tree, [e for e in entries if e["id"] in aside], stats)
            return
        with ownership_lock(repo, OWNERSHIP_WAIT_CHECKED):
            graph: list[dict[str, Any]] = []
            if any(e["bead"] for e in picked):
                sync(repo)
                graph = Beads(repo).all()
            lost = False
            for entry in picked:
                if not entry["bead"]:
                    continue
                bead = next((b for b in graph if b["id"] == entry["bead"]), None)
                try:
                    if not bead:
                        raise Fail(f"bead {entry['bead']} not found")
                    require_owner(bead, entry["owner"])
                except Fail as error:
                    decide(repo, entry, {"status": "error", "error": str(error),
                                         "code": error.code})
                    lost = True
            if lost:
                continue  # rebuild the batch without the entries that lost ownership
            receipts = []
            for entry in picked:
                bead = next((b for b in graph if b["id"] == entry["bead"]), {})
                data = {"bead": entry["bead"], "head": entry["head"], "commits": entry["commits"],
                        "history": list(dict.fromkeys(
                            (meta(bead, "enact_commits") or "").split() + entry["commits"])),
                        "runs": bead_runs(bead, graph) if bead else [], "ci": entry["ci"],
                        "queue": entry["id"]}
                receipt = delivery_receipts(repo) / f"{entry['head']}.json"
                # Written before the push, so a lost push response is recoverable.
                write_json(receipt, data)
                receipts.append((entry, receipt, data))
            tip = repo.git("rev-parse", "HEAD", cwd=tree)
            if repo.git_ok("push", "-q", cfg.remote, f"{tip}:refs/heads/{cfg.staging}", cwd=tree):
                for entry, receipt, data in receipts:
                    decide(repo, entry, {"status": "landed", "receipt": str(receipt),
                                         "delivery": data, "staging": tip,
                                         "batch": len(picked), "checks": stats["checks"]})
                return
            for _, receipt, _ in receipts:
                receipt.unlink(missing_ok=True)
        log(f"merge queue: push to {cfg.staging} rejected (attempt {attempt + 1}); rebuilding")
    for entry in entries:
        if not verdict_path(repo, entry["id"]).exists():
            decide(repo, entry, {"status": "error", "code": 1, "error":
                                 f"could not push to {cfg.staging} after repeated attempts"})


def queue_push(repo: Repo, args: argparse.Namespace, base: str, head: str) -> dict[str, Any]:
    """Queue `base..head`, already rebased onto staging, and wait for its verdict, running
    the queue whenever no other push on this host is."""
    owner = ""
    if args.bead:
        owner = as_owner(require_actor(None), Beads(repo).show(args.bead))
    entry = {"id": f"{time.time_ns():020d}-{os.getpid()}", "top": str(repo.top),
             "base": base, "head": head, "bead": args.bead, "owner": owner,
             "files": repo.git("diff", "--name-only", "--no-renames", base, head).splitlines(),
             "ci": bool(repo.config.slow and not args.no_ci), "pid": os.getpid(),
             "enqueued": time.time()}
    folder = queue_dir(repo)
    for old in queued(repo):
        if old["top"] == entry["top"]:
            drop_entry(repo, old["id"])
    write_json(folder / f"{entry['id']}.json", entry)
    lock = repo.state / "queue.lock"
    try:
        while not verdict_path(repo, entry["id"]).exists():
            if not (folder / f"{entry['id']}.json").exists():
                raise Fail("another push from this worktree replaced this one in the queue")
            guard = try_lock(lock)
            if guard is None:
                time.sleep(QUEUE_POLL_SECONDS)
                continue
            try:
                run_queue(repo, entry["id"])
            finally:
                release_lock(lock, guard)
        return json.loads(verdict_path(repo, entry["id"]).read_text(encoding="utf-8"))
    finally:
        drop_entry(repo, entry["id"])


def cmd_push(args: argparse.Namespace) -> dict[str, Any]:
    """Rebase, run the fast check and publish to staging.

    The ownership lock covers only the Beads bookkeeping: the first ownership
    check, and the final check, push and delivery record. The rebase and the
    fast check run outside it, so other pushes, claims and syncs on this host
    proceed while a check runs; a push that loses the race to staging is
    rejected by the remote and re-integrates, as before.

    With `[push] queue = true`, the fast check runs in the host's merge queue
    instead (`queue_push`), once for a batch of pushes.
    """
    repo = find_repo()
    enact_update = update_enact()
    with ownership_lock(repo):
        sync(repo)
        assigned = worktree_bead(repo, repo.top)
        if assigned and args.bead and args.bead != assigned["id"]:
            raise Fail("--bead differs from this worktree's assignment", code=3)
        args.bead = args.bead or (assigned["id"] if assigned else None)
        if args.bead:
            require_owner(Beads(repo).show(args.bead), require_actor(None))
    cfg = repo.config
    if repo.git("status", "--porcelain", "--untracked-files=no"):
        raise Fail("working tree has uncommitted changes; commit them first", code=2)
    fetch(repo)
    recovered = recover_deliveries(repo)
    staging = repo.remote_ref(cfg.staging)
    result: dict[str, Any] = {"pushed": False, "reason": "nothing to push"}
    pending_ci: Path | None = None
    # The commit the fast check last passed. After a push loses the race to
    # staging, the check runs again with ENACT_CHECKED set to it, so a project's
    # check may skip what the commits that landed meanwhile cannot affect.
    checked = ""
    queueing = cfg.push_queue and cfg.fast and not args.skip_checks
    for attempt in range(6):
        integrate(repo)
        before = repo.git("rev-parse", staging)
        head = repo.git("rev-parse", "HEAD")
        if head == before:
            if not recovered:
                return result
            result["recovered"] = recovered
            break
        # The integrator replays commits one by one, so a branch that merged a diverged
        # master (rare: someone bypassed staging) takes the direct path.
        if queueing and not repo.git("rev-list", "--merges", f"{before}..{head}"):
            verdict = queue_push(repo, args, before, head)
            if verdict["status"] == "conflict":
                log("a branch queued ahead of this one conflicts with it; re-integrating")
                fetch(repo)
                continue
            if verdict["status"] == "failed":
                sys.stderr.write(verdict["output"])
                raise Fail("fast checks failed; fix them and run `enact push` again", code=2)
            if verdict["status"] != "landed":
                raise Fail(verdict["error"], code=verdict["code"])
            data = verdict["delivery"]
            receipt = Path(verdict["receipt"])
            with ownership_lock(repo, OWNERSHIP_WAIT_CHECKED):
                result = {"pushed": True, **finish_delivery(repo, receipt, data, start=False)}
            result["queue"] = {k: verdict[k] for k in ("batch", "checks") if k in verdict}
            # The tree now holds what landed: the queue may have replayed its commits.
            repo.git("reset", "-q", "--hard", data["head"])
            bead = Beads(repo).show(args.bead) if args.bead else {}
            pending_ci = receipt if data["ci"] else None
            if recovered:
                result["recovered"] = recovered
            break
        if cfg.fast and not args.skip_checks:
            if run_shell(cfg.fast, repo.top, {"ENACT_CHECKED": checked} if checked else None):
                raise Fail("fast checks failed; fix them and run `enact push` again", code=2)
            checked = head
        commits = repo.git("rev-list", "--reverse", f"{before}..{head}").split()
        with ownership_lock(repo, OWNERSHIP_WAIT_CHECKED if checked else OWNERSHIP_WAIT):
            graph = Beads(repo).all() if args.bead else []
            bead = next((b for b in graph if b["id"] == args.bead), {})
            if args.bead and not bead:
                raise Fail(f"bead {args.bead} not found")
            data = {"bead": args.bead, "head": head, "commits": commits,
                    "history": list(dict.fromkeys(
                        (meta(bead, "enact_commits") or "").split() + commits)),
                    "runs": bead_runs(bead, graph), "ci": bool(cfg.slow and not args.no_ci)}
            receipt = delivery_receipts(repo) / f"{head}.json"
            # Write before the remote mutation, so a lost push response is recoverable.
            write_json(receipt, data)
            if args.bead:
                sync(repo)
                require_owner(Beads(repo).show(args.bead), require_actor(None))
            pushed = repo.git_ok("push", "-q", cfg.remote, f"HEAD:refs/heads/{cfg.staging}")
            if pushed:
                result = {"pushed": True, **finish_delivery(repo, receipt, data, start=False)}
        if pushed:
            pending_ci = receipt if data["ci"] else None
            if recovered:
                result["recovered"] = recovered
            break
        log(f"push to {cfg.staging} rejected (attempt {attempt + 1}); re-integrating")
        fetch(repo)
        recovered += recover_deliveries(repo)
    else:
        raise Fail(f"could not push to {cfg.staging} after repeated attempts")
    graph = Beads(repo).all() if pending_ci else []
    # A held run's worker finishes what is in flight but starts no repair.
    if (pending_ci and not (bead and stopped_runs(bead, graph))
            and not bead_watched(repo, graph, bead)):
        result["watch_ci"] = watch_ci_notice(result["staging"])
    elif result.get("pushed") and not args.bead:
        result["title"] = TITLE_NOTICE
    result["local_branches"] = track_local_branches(repo)
    result["enact_update"] = enact_update
    branch = repo.git("rev-parse", "--abbrev-ref", "HEAD")
    in_enact_tree = (
        repo.top.resolve().parent == repo.worktree_root().resolve() and repo.top != repo.main
    )
    runner = ci_runner(repo) if pending_ci else None
    try:
        if in_enact_tree and not args.keep:
            if repo.git("status", "--porcelain"):
                result["kept_worktree"] = (
                    f"{repo.top} has untracked files, so it was not deleted. Delete "
                    f"or move them, then run `enact worktree remove {repo.top.name}`.")
            else:
                os.chdir(repo.main)
                ended = remove_worktree(repo, repo.top,
                                        branch if branch.startswith("enact/") else None)
                result["removed_worktree"] = str(repo.top)
                if ended:
                    result["ended_processes"] = ended
    finally:
        # Slow CI starts after the remove hook, which may save the build
        # caches the runner's fresh worktree warms itself from.
        if pending_ci:
            result["ci"] = start_ci(repo, runner)
            pending_ci.unlink(missing_ok=True)
    return result


# ---------------------------------------------------------------------------
# Slow CI runner


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def try_lock(path: Path) -> BinaryIO | None:
    """Hold an OS lock and publish a complete PID for legacy runners.

    The guard inode is permanent: unlinking it would let contenders lock
    different files. The OS releases it even when a runner is killed.
    """
    guard = path.with_name(path.name + ".guard").open("a+b")
    acquired = False
    try:
        try:
            if WINDOWS:
                import msvcrt

                if os.fstat(guard.fileno()).st_size == 0:
                    guard.write(b"\0")
                    guard.flush()
                guard.seek(0)
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return None
            raise
        try:
            with path.open() as existing:
                observed = os.fstat(existing.fileno())
                pid = int(existing.read().strip())
            # An incomplete legacy marker may still have a live writer.
            if pid <= 0 or pid_alive(pid):
                return None
            if not os.path.samestat(observed, path.stat()):
                return None
            path.unlink()
        except FileNotFoundError:
            pass
        except ValueError:
            return None
        # A hard link is create-only and exposes no empty-file interval to
        # old runners, which still use O_EXCL and read this PID directly.
        fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            try:
                path.hardlink_to(temporary)
            except FileExistsError:
                return None
        finally:
            temporary.unlink(missing_ok=True)
        acquired = True
        return guard
    finally:
        if not acquired:
            guard.close()


def release_lock(path: Path, guard: BinaryIO) -> None:
    try:
        try:
            if path.read_text().strip() == str(os.getpid()):
                path.unlink()
        except FileNotFoundError:
            pass
    finally:
        guard.close()


def ci_status(repo: Repo) -> dict[str, Any]:
    lock = repo.state / "ci.lock"
    pid = 0
    try:
        pid = int(lock.read_text().strip() or "0")
    except (FileNotFoundError, ValueError):
        pass
    last = repo.state / "ci-last"
    outcome = repo.state / "ci-result.json"
    promotion = repo.state / "ci-promotion.json"
    try:
        pending = json.loads(promotion.read_text())
    except FileNotFoundError:
        pending = None  # the runner may retire its receipt during status reads
    return {
        "result": json.loads(outcome.read_text()) if outcome.exists() else None,
        "pending_promotion": pending,
        "running": pid_alive(pid),
        "pid": pid or None,
        "last_tested": last.read_text().strip() if last.exists() else None,
        "log": str(repo.state / "ci.log"),
    }


def vizier_active(repo: Repo, graph: list[dict[str, Any]]) -> bool:
    """A vizier loop on this machine will pick up a Steward bead this machine
    files: some run that is not paused or blocked has a route here whose
    vizier renewed its lease recently. Whether the epic is open or its
    delivery complete does not matter; a route alone proves nothing, since
    routes outlive the viziers that wrote them."""
    return any(bead.get("issue_type") == "epic" and run_live(repo, bead, graph)
               for bead in graph)


def watch_ci_notice(sha: str) -> str:
    return (
        "No Enact vizier is running to repair a slow-CI failure, so this session "
        f"owns it. Run `enact ci --wait {sha}` (600000 ms tool timeout) until it "
        "reports `passed`. On `failed`, claim the Steward bead with "
        f"`enact claim <bead>`, follow {ROLES_DIR / 'steward.md'}, and watch "
        "your fix the same way. If a result lists `other_os_stewards`, your "
        "commits also failed slow CI on another OS, which a machine on that OS "
        "repairs first: read its bead's log, and if the cause is plain from it, "
        "claim the unassigned bead and repair it as a Steward; otherwise comment "
        "your analysis on the bead and name it in your final reply. "
        "Title this session `🔮 <summary>` while waiting, "
        "`🚒 <summary>` while repairing, and `✅ <summary>` once CI passes "
        "(`⛔` if you stop without it)."
    )


def steward_covers(repo: Repo, bead: dict[str, Any], sha: str) -> bool:
    green, sep, tip = (meta(bead, "enact_ci_range") or "").partition("..")
    return bool(sep) and repo.git_ok("merge-base", "--is-ancestor", sha, tip) and not (
        repo.git_ok("merge-base", "--is-ancestor", sha, green))


def ci_wait(repo: Repo, sha: str, seconds: int, interval: int) -> dict[str, Any]:
    """Watch one delivery for a session that no vizier backs. Restarts a
    runner that died (for example across sleep) while staging is untested."""
    deadline = time.monotonic() + max(seconds, 0)
    master = repo.remote_ref(repo.config.master)
    while True:
        sync(repo)
        fetch(repo)
        graph = Beads(repo).all()
        covering = [b for b in graph if role_of(b) == "steward"
                    and b.get("status") != "closed" and steward_covers(repo, b, sha)]
        # Another OS failing does not stop this one promoting master, but the
        # session that pushed should know its commits broke a peer machine.
        elsewhere = {"other_os_stewards": [
            {"id": b["id"], "os": meta(b, "enact_ci_os"), "host": meta(b, "enact_ci_host"),
             "assignee": b.get("assignee") or None}
            for b in covering if meta(b, "enact_ci_os") not in (None, ci_os())]}
        elsewhere = elsewhere if elsewhere["other_os_stewards"] else {}
        if repo.git_ok("merge-base", "--is-ancestor", sha, master):
            return {"sha": sha, "state": "passed", **elsewhere}
        # A Steward someone else holds is already being repaired: keep waiting.
        stewards = [b["id"] for b in covering if meta(b, "enact_ci_os") == ci_os()
                    and (b.get("assignee") or "") in session_aliases() | {""}]
        if stewards:
            return {"sha": sha, "state": "failed", "stewards": stewards,
                    "vizier_active": vizier_active(repo, graph), **elsewhere}
        status = ci_status(repo)
        if not status["running"] and status["last_tested"] != repo.git(
                "rev-parse", repo.remote_ref(repo.config.staging)):
            status = start_ci(repo)
        if time.monotonic() >= deadline:
            return {"sha": sha, "state": "waiting", "ci": status, **elsewhere}
        time.sleep(min(interval, max(deadline - time.monotonic(), 1)))


def ci_runner(repo: Repo) -> Path:
    """An immutable copy of this script in the git common directory, which
    the runner launches from: the caller may be delivering Enact itself and
    remove its own worktree, this script's file with it."""
    source = Path(__file__).read_bytes()
    runner = repo.state / f"ci-runner-{hashlib.sha256(source).hexdigest()}.py"
    if not runner.exists():
        temp = runner.with_suffix(f".{os.getpid()}.tmp")
        temp.write_bytes(source)
        temp.replace(runner)
    return runner


def start_ci(repo: Repo, runner: Path | None = None) -> dict[str, Any]:
    status = ci_status(repo)
    if status["running"]:
        return {"started": False, "reason": "runner already active", **status}
    runner = runner or ci_runner(repo)
    cmd = [sys.executable, str(runner), "ci"]
    kwargs: dict[str, Any] = {}
    if WINDOWS:
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(repo.state / "ci.log", "a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd, cwd=repo.main, stdout=log_file, stderr=log_file, stdin=subprocess.DEVNULL, **kwargs
        )
    return {"started": True, "pid": proc.pid, "log": str(repo.state / "ci.log")}


def ci_os() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)


def enact_checkout() -> Path:
    return stable_checkout(ENACT_ROOT)


def update_enact() -> str:
    """Fast-forward the Enact checkout to its master, so every machine runs
    the helper and roles slow CI last passed. Best effort: a checkout that is
    dirty, off master or diverged is left alone and reported. The change takes
    effect from the next command."""
    if os.environ.get("ENACT_NO_SELF_UPDATE"):
        return "disabled"
    root = enact_checkout()
    if not (root / ".git").exists():
        return "not a git checkout"
    git = lambda *args: run(["git", *args], root, check=False)  # noqa: E731
    if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "master":
        return "not on master"
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "local changes"
    before = git("rev-parse", "HEAD").stdout.strip()
    pulled = git("pull", "--ff-only", "-q")
    if pulled.returncode != 0:
        return f"pull failed: {(pulled.stderr or pulled.stdout).strip()[-200:]}"
    return "updated" if git("rev-parse", "HEAD").stdout.strip() != before else "current"


UNOWNED_NOTE = (
    "\n\nNo vizier on this machine has a fresh lease on a run this failure "
    "affects, so nobody is repairing it (`enact_unowned=true`). Claim it with "
    "`enact claim <bead>` and repair it as a Steward; claiming clears the flag."
)


def report_ci_failure(repo: Repo, tip: str, green: str, log_tail: str) -> str:
    beads = Beads(repo)
    sync(repo)
    os_name = ci_os()
    graph = beads.all()
    runs = list(dict.fromkeys(
        run_id for bead in graph
        if any(repo.git_ok("merge-base", "--is-ancestor", sha, tip)
               and not repo.git_ok("merge-base", "--is-ancestor", sha, green)
               for sha in (meta(bead, "enact_commits") or "").split())
        for run_id in bead_runs(bead, graph)
    ))
    open_stewards = [
        b
        for b in beads.json("list", "--label", "role:steward", "-n", "0") or []
        if b.get("status") != "closed" and meta(b, "enact_ci_os") == os_name
    ]
    body = (
        f"Slow CI (`{repo.config.slow}`) failed on {host_name()} ({os_name}) at "
        f"{tip}. Failing range since last passing commit: {green}..{tip}.\n\n"
        f"Log tail:\n```\n{log_tail}\n```"
    )
    if open_stewards:
        bead_id = open_stewards[0]["id"]
        # Preserve inferred legacy associations before replacing the old range.
        runs += [b["id"] for b in graph if b.get("issue_type") == "epic"
                 and bead_id in run_delivery(repo, graph, b["id"])["stewards"]]
        runs = list(dict.fromkeys((meta(open_stewards[0], "enact_runs") or "").split() + runs))
        changes = {"enact_runs": " ".join(runs), "enact_ci_range": f"{green}..{tip}"}
        if not open_stewards[0].get("assignee") and not runs_watched(repo, graph, runs):
            body += UNOWNED_NOTE
            changes["enact_unowned"] = "true"
        beads.run("comments", "add", bead_id, body)
        beads.set_meta(bead_id, changes)
    else:
        unowned = not runs_watched(repo, graph, runs)
        created = beads.json(
            "create",
            "--title", f"Fix slow CI failure on {os_name}",
            "--description", body + (UNOWNED_NOTE if unowned else ""),
            "--acceptance", f"`{repo.config.slow}` passes on {os_name} at the staging tip.",
            "--priority", "0",
            "--labels", "role:steward",
            "--metadata", json.dumps({"enact_ci_os": os_name, "enact_ci_host": host_name(),
                                     "enact_ci_range": f"{green}..{tip}",
                                     "enact_runs": " ".join(runs),
                                     **({"enact_unowned": "true"} if unowned else {})}),
        )
        bead_id = created["id"]
    sync(repo)
    return bead_id


def superseding_repair(repo: Repo, tip: str) -> str | None:
    """A Steward repair for this OS that reached staging after the failing tip.
    The failure is then stale: the runner's loop retests the newer tip, which
    files or comments only if the repair did not fix it."""
    fetch(repo)
    staging = repo.remote_ref(repo.config.staging)
    newer = set(repo.git("rev-list", f"{tip}..{staging}", cwd=repo.main).split())
    if not newer:
        return None
    sync(repo)
    for bead in Beads(repo).all():
        if (role_of(bead) == "steward" and meta(bead, "enact_ci_os") == ci_os()
                and newer & set((meta(bead, "enact_commits") or "").split())):
            return bead["id"]
    return None


def ci_pass(repo: Repo, tip: str) -> bool:
    cfg = repo.config
    master = repo.remote_ref(cfg.master)
    if repo.git_ok("merge-base", "--is-ancestor", tip, master):
        return True  # a peer already promoted this candidate or its descendant
    if repo.git_ok("rev-parse", "-q", "--verify", master) and not repo.git_ok(
        "merge-base", "--is-ancestor", master, tip
    ):
        log(f"{master} is not an ancestor of {tip}; leaving master alone")
        return False
    if not repo.git_ok("push", "-q", cfg.remote, f"{tip}:refs/heads/{cfg.master}"):
        return False
    track_local_branches(repo)
    return True


def retry_ci_promotion(repo: Repo) -> dict[str, Any] | None:
    """Keep a passing candidate independent of newer, possibly failing tests."""
    receipt = repo.state / "ci-promotion.json"
    outcome = repo.state / "ci-result.json"
    previous = json.loads(outcome.read_text()) if outcome.exists() else {}
    # Upgrade runners that only retained the last test result.
    if not receipt.exists() and previous.get("passed") and not previous.get("master_advanced"):
        write_json(receipt, {"tip": previous["tip"]})
    if not receipt.exists():
        return None
    tip = json.loads(receipt.read_text())["tip"]
    result = {"tip": tip, "passed": True, "master_advanced": ci_pass(repo, tip)}
    if previous.get("tip") == tip:
        write_json(outcome, result)
    if result["master_advanced"]:
        receipt.unlink(missing_ok=True)
    return result


def cmd_ci(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    cfg = repo.config
    if args.detach:
        return start_ci(repo)
    if args.status:
        return ci_status(repo)
    if args.wait:
        return ci_wait(repo, repo.git("rev-parse", args.wait), args.timeout, args.interval)
    if args.check_worker:
        return ci_check_worker(repo, args.check_worker, args.coordinator)
    if not cfg.slow:
        raise Fail("no [checks] slow command in .enact.toml")
    lock = repo.state / "ci.lock"
    last_file = repo.state / "ci-last"
    results: list[dict[str, Any]] = []
    while True:
        guard = try_lock(lock)
        if guard is None:
            return {"ran": results, "reason": "another runner holds the lock"}
        try:
            fetch(repo)
            # No check can be live under the lock, so stale CI trees go too.
            # Cleanup must never cost a check, so failures are only logged.
            try:
                sweep_worktrees(repo, ci_trees=True)
            except (Fail, OSError) as error:
                log(f"worktree sweep failed: {error}")
            promotion = retry_ci_promotion(repo)
            if promotion:
                results.append(promotion)
            while True:
                fetch(repo)
                tip = repo.git("rev-parse", repo.remote_ref(cfg.staging), cwd=repo.main)
                last = last_file.read_text().strip() if last_file.exists() else ""
                outcome = repo.state / "ci-result.json"
                previous = json.loads(outcome.read_text()) if outcome.exists() else {}
                if tip == last:
                    if previous.get("tip") == tip or repo.git_ok(
                        "merge-base", "--is-ancestor", tip, repo.remote_ref(cfg.master)
                    ):
                        break
                    # Legacy last-tested files have no outcome. Recheck an
                    # unpromoted tip once rather than stranding an old run.
                if ci_wait_for_gap(repo):
                    continue  # new commits may have arrived while waiting: retest the newest tip
                result = ci_supervise_check(repo, tip)
                results.append(result)
        finally:
            release_lock(lock, guard)
        fetch(repo)
        tip = repo.git("rev-parse", repo.remote_ref(cfg.staging), cwd=repo.main)
        if tip == (last_file.read_text().strip() if last_file.exists() else ""):
            return {"ran": results}


def ci_gap_remaining(finished: float | None, now_time: float, gap: int) -> float:
    """Seconds still to wait before the next slow-CI run may start, given when the last one
    finished (wall-clock seconds, None if never) and the configured minimum gap."""
    if finished is None or gap <= 0:
        return 0.0
    return min(max(finished + gap - now_time, 0.0), float(gap))


def ci_wait_for_gap(repo: Repo) -> bool:
    """Sleep out `[checks] slow_min_gap` since the last run ended, holding the runner lock so
    no second runner starts. Returns True if it waited, so the caller refetches `staging` and
    tests whatever the tip is by then."""
    finished_file = repo.state / "ci-finished"
    try:
        finished: float | None = float(finished_file.read_text().strip())
    except (OSError, ValueError):
        finished = None
    remaining = ci_gap_remaining(finished, time.time(), repo.config.slow_min_gap)
    if remaining <= 0:
        return False
    log(f"slow CI waits {round(remaining)} s (slow_min_gap) so commits batch into one run")
    time.sleep(remaining)
    return True


def ci_supervise_check(repo: Repo, tip: str) -> dict[str, Any]:
    """Publish the check keeper's PID before authorizing any protected work."""
    lock = repo.state / "ci.lock"
    kwargs: dict[str, Any] = {}
    if WINDOWS:
        kwargs["creationflags"] = 0x00000200  # NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    output = repo.state / "ci-check.log"
    with output.open("w", encoding="utf-8") as handle:
        with subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "ci",
             "--check-worker", tip, "--coordinator", str(os.getpid())],
            cwd=repo.main, stdin=subprocess.PIPE, stdout=handle, stderr=handle,
            **kwargs,
        ) as worker:
            # The coordinator still holds the OS guard. Until this atomic
            # handoff the child can only wait for permission or exit on EOF.
            write_json(lock, worker.pid)
            worker.stdin.write(b"run\n")
            worker.stdin.close()
            worker.wait()
    sys.stderr.write(output.read_text(encoding="utf-8", errors="replace")[-20000:])
    if worker.returncode:
        raise Fail(f"slow-CI check keeper exited {worker.returncode}; see {output}")
    return json.loads((repo.state / "ci-result.json").read_text())


def ci_check_worker(repo: Repo, tip: str, coordinator: int) -> dict[str, Any]:
    """Keep the PID lease through checks, cleanup and durable result recording."""
    lock = repo.state / "ci.lock"
    if sys.stdin.buffer.readline() != b"run\n":
        return {"ran": False}  # coordinator died before authorizing this child
    if lock.read_text().strip() != str(os.getpid()):
        raise Fail("check keeper was not given CI ownership")
    try:
        result = ci_run_once(repo, tip)
        write_json(repo.state / "ci-result.json", result)
        (repo.state / "ci-last").write_text(tip)
        return result
    finally:
        (repo.state / "ci-finished").write_text(str(time.time()))
        # Restore before exiting, so legacy contenders never see a dead
        # keeper while the live coordinator still owns the OS guard.
        # If the coordinator died, our stale PID is recoverable after exit.
        if pid_alive(coordinator):
            write_json(lock, coordinator)


def run_bounded(command: str, cwd: Path, timeout: int) -> tuple[int, str]:
    """Run a project-configured check, its output as one stream, killing its whole process
    tree once `timeout` seconds pass: a hung grandchild (a stuck exec, a server never told to
    stop) fails the check instead of holding the runner. Returns (exit code, output)."""
    args, shell = shell_args(command)
    group: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        args, shell=shell, cwd=cwd, text=True, encoding="utf-8", errors="replace",
        # One stream, in order: the log tail must end where the check failed.
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **group,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        return proc.returncode, output or ""
    except subprocess.TimeoutExpired:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        try:
            output, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # a descendant left the group and holds the pipe
            proc.kill()
            output = ""
        note = f"enact: killed the check after {timeout} s (slow_timeout); it hung or ran away"
        return 124, f"{output or ''}\n{note}\n"


def ci_run_once(repo: Repo, tip: str) -> dict[str, Any]:
    cfg = repo.config
    green = repo.git("rev-parse", repo.remote_ref(cfg.master), cwd=repo.main, check=False) or "?"
    promotion = repo.state / "ci-promotion.json"
    if promotion.exists():
        passed = json.loads(promotion.read_text())["tip"]
        if repo.git_ok("merge-base", "--is-ancestor", green, passed) and repo.git_ok(
            "merge-base", "--is-ancestor", passed, tip
        ):
            green = passed  # a later failure must not blame already-passing work
    name = f"ci-{tip[:12]}"
    stale = repo.worktree_root() / name
    if stale.exists():
        remove_worktree(repo, stale, None)
    path = create_worktree(repo, name, None, tip)
    # Timed, so a Sage can hold the slow suite to its budget from the log.
    started = time.monotonic()
    log(f"slow CI on {tip[:12]} in {path} at {now()}")
    try:
        returncode, output = run_bounded(cfg.slow, path, cfg.slow_timeout)
    finally:
        remove_worktree(repo, path, None)
    seconds = round(time.monotonic() - started)
    sys.stderr.write(output[-20000:])
    if returncode == 0:
        write_json(promotion, {"tip": tip})
        advanced = ci_pass(repo, tip)
        if advanced:
            promotion.unlink(missing_ok=True)
        log(f"slow CI passed at {tip[:12]} in {seconds} s; master advanced: {advanced}")
        return {"tip": tip, "passed": True, "master_advanced": advanced, "seconds": seconds}
    tail = "\n".join(output.strip().splitlines()[-60:])
    repaired = superseding_repair(repo, tip)
    if repaired:
        log(f"slow CI failed at {tip[:12]}, which predates {repaired}'s repair "
            "on staging; retesting the newer tip before filing")
        return {"tip": tip, "passed": False, "superseded_by": repaired}
    bead_id = report_ci_failure(repo, tip, green, tail)
    try:
        for run in prune_runs(repo):
            log(f"pruned run {run['epic']} ({run['state']}): {run['reason']}")
    except Fail as failure:
        log(f"could not prune stale runs: {failure}")
    log(f"slow CI failed at {tip[:12]} in {seconds} s; steward bead {bead_id}")
    return {"tip": tip, "passed": False, "steward": bead_id, "seconds": seconds}


# ---------------------------------------------------------------------------
# Machine status and handoffs


def spawned_here(bead: dict[str, Any], host: str, host_provider: str) -> bool:
    """Spawned by a vizier in this host app on this machine. Beads reserved
    before provider/app routing existed remain visible for inspection."""
    spawn_provider = meta(bead, "enact_spawn_provider")
    spawn_app = meta(bead, "enact_spawn_app")
    return meta(bead, "enact_spawn_host") == host and (
        not spawn_provider or spawn_provider == host_provider
    ) and (not spawn_app or spawn_app == os.environ.get("ENACT_APP", host_provider))


def vizier_routes(bead: dict[str, Any], graph: list[dict[str, Any]]) -> list[dict[str, Any]]:
    routes = run_routes(bead)
    # Old runs can be inspected safely, but recovery needs registration
    # carrying authorization. Include closed assignments for final CI.
    for worker in graph:
        session = meta(worker, "enact_vizier")
        if session and routed_here(worker) and bead["id"] in bead_runs(worker, graph) and not any(
                route.get("session") == session for route in routes):
            routes.append({"session": session, "authorized": False,
                           **{key: meta(worker, "enact_spawn_" + key)
                              for key in ("host", "provider", "app", "project")}})
    return routes


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    """This machine and host app's Enact state: a Claude session cannot see
    Codex threads, and vice versa."""
    repo = find_repo()
    sync(repo)
    local_branches = fetch(repo, check=False)
    recover_deliveries(repo)
    graph = Beads(repo).all()
    here = host_name()
    ours = [b for b in graph if spawned_here(b, here, provider())]
    busy = busy_sessions(graph)
    working = [
        {
            "bead": b["id"],
            "title": b.get("title"),
            "assignee": b.get("assignee"),
            "status": b.get("status"),
            "session": meta(b, "enact_session"),
            "vizier": meta(b, "enact_vizier"),
            "runs": bead_runs(b, graph),
            "recoverable": not stopped_runs(b, graph),
            "updated_at": b.get("updated_at"),
            "routing": {key: meta(b, "enact_spawn_" + key)
                        for key in ("host", "provider", "app", "project")},
            "routing_known": routed_here(b),
            "reservation": meta(b, "enact_reservation"),
            "worktree": meta(b, "enact_worktree"),
            "worktree_host": meta(b, "enact_worktree_host"),
            "branch": meta(b, "enact_branch"),
            "recovery": recovery_records(b),
        }
        for b in ours
        if b.get("status") != "closed" and b.get("assignee")
    ]
    unarchived = [
        {"bead": b["id"], "session": meta(b, "enact_session"),
         "routing": {key: meta(b, "enact_spawn_" + key)
                     for key in ("host", "provider", "app", "project")},
         "routing_known": routed_here(b)}
        for b in ours
        if b.get("status") == "closed"
        and meta(b, "enact_session")
        and meta(b, "enact_session") not in busy
        and meta(b, "enact_archived") != "true"
    ]
    ancestry: dict[str, set[str]] = {}
    deliveries = {b["id"]: run_delivery(repo, graph, b["id"], ancestry)
                  for b in graph if b.get("issue_type") == "epic"}
    active_runs = []
    for bead in graph:
        if bead.get("issue_type") != "epic":
            continue
        routes = vizier_routes(bead, graph)
        routes = [r for r in routes if r.get("host") == here and r.get("provider") == provider()
                  and r.get("app") == os.environ.get("ENACT_APP", provider())]
        if not routes:
            continue
        delivery = deliveries[bead["id"]]
        if run_finished(bead) or (bead.get("status") == "closed" and delivery["complete"]):
            continue
        halted = stopped_runs(bead, graph)
        control = run_control(halted[0] if halted else bead)
        routes, stale = route_leases(repo, bead["id"], routes)
        active_runs.append({"epic": bead["id"], "control": control, "routes": routes,
                            "stale": stale, "delivery": delivery, "recoverable": not halted})
    pending_deliveries = {epic: delivery for epic, delivery in deliveries.items()
                          if not delivery["complete"]}
    staging = repo.remote_ref(repo.config.staging)
    master = repo.remote_ref(repo.config.master)
    lag = repo.git("rev-list", "--count", f"{master}..{staging}", check=False)
    stale = [item["bead"]["id"] for item in stale_runs(repo, graph, deliveries)]
    return {
        "host": here,
        "provider": provider(),
        "working": working,
        "active_runs": active_runs,
        "unarchived": unarchived,
        "ci": ci_status(repo),
        "master_lag": int(lag) if lag.isdigit() else None,
        "staging": repo.git("rev-parse", staging, check=False),
        "pending_deliveries": pending_deliveries,
        "local_branches": local_branches,
        "justiciar": justiciar_state(repo),
        "open_stewards": [
            b["id"] for b in graph if role_of(b) == "steward" and b.get("status") != "closed"
        ],
        "unowned_stewards": unowned_stewards(repo, graph, deliveries),
        "stale_runs": stale,
        **({"hint": "Runs with no fresh vizier lease: `enact run prune` finishes "
                    "the delivered ones and pauses the rest."} if stale else {}),
    }


def routed_here(bead: dict[str, Any]) -> bool:
    # Missing routing is uncertainty, never evidence a chat is absent.
    return (meta(bead, "enact_spawn_host") == host_name() and
            meta(bead, "enact_spawn_provider") == provider() and
            meta(bead, "enact_spawn_app") == os.environ.get("ENACT_APP", provider()) and
            bool(meta(bead, "enact_spawn_project")))


@serialized
def cmd_release(args: argparse.Namespace) -> dict[str, Any]:
    """Relinquish work, or hand off an explicitly confirmed inactive owner."""
    repo = find_repo()
    beads = Beads(repo)
    actor = require_actor(args.actor)
    sync(repo)
    bead = beads.show(args.bead)
    require_running(bead, beads.all())
    actor = as_owner(actor, bead)
    owner = bead.get("assignee")
    if not owner or bead.get("status") == "closed":
        raise Fail("no active assignment to release", code=3)
    own_reservation = (owner.startswith("vizier:") and
                       meta(bead, "enact_vizier") == actor and
                       not meta(bead, "enact_session"))
    if owner.startswith("vizier:"):
        token = meta(bead, "enact_reservation")
        if token and args.reservation != token:
            raise Fail("reservation token missing or changed; recheck before releasing", code=3)
    if owner != actor and not own_reservation:
        target = meta(bead, "enact_session") or meta(bead, "enact_vizier")
        if (not routed_here(bead) or args.expected_owner != owner or
                not target or args.expected_session != target or
                not args.inactive or not args.evidence):
            raise Fail("release requires the current owner, routed inactive session "
                       "and successful lookup evidence; unknown is not inactive", code=3)
    path = meta(bead, "enact_worktree")
    branch = meta(bead, "enact_branch")
    work_host = meta(bead, "enact_worktree_host")
    # Legacy sessions did not record a worktree pointer. Discover only a
    # local, correctly registered tree; never guess a remote filesystem path.
    legacy = repo.worktree_root() / args.bead
    if not path and meta(bead, "enact_spawn_host") == host_name() and legacy.exists():
        path, branch, work_host = str(legacy), f"enact/{args.bead}", host_name()
        verified_worktree(repo, legacy, branch)
    record = {"owner": owner, "session": meta(bead, "enact_session"),
              "vizier": meta(bead, "enact_vizier"),
              "provider": meta(bead, "enact_spawn_provider"),
              "app": meta(bead, "enact_spawn_app"),
              "project": meta(bead, "enact_spawn_project"),
              "spawn_host": meta(bead, "enact_spawn_host"),
              "host": work_host, "path": path, "branch": branch,
              "inactive": args.inactive or "relinquished",
              "evidence": args.evidence or "current owner explicitly released",
              "released": now(), "reason": args.reason}
    if path and work_host == host_name() and Path(path).exists():
        verified_worktree(repo, Path(path), branch)
        record["head"] = repo.git("rev-parse", "HEAD", cwd=Path(path))
    records = recovery_records(bead) + [record]
    reassign(
        beads, args.bead, "--assignee", "", "--status", "open",
        "--set-metadata", "enact_recovery=" + json.dumps(records),
        "--unset-metadata", "enact_vizier", "--unset-metadata", "enact_session",
        "--unset-metadata", "enact_actor", "--unset-metadata", "enact_archived",
        "--unset-metadata", "enact_reservation", "--unset-metadata", "enact_pool_session",
        actor=actor)
    result = sync(repo)
    saved = beads.show(args.bead)
    if saved.get("assignee") or recovery_records(saved) != records:
        raise Fail("release lost during sync; recheck ownership", code=3)
    beads.run("comments", "add", args.bead, f"Released by Enact: {args.reason}", actor=actor)
    sync(repo)
    return {"released": args.bead, "recovery": record, "sync": result}


# ---------------------------------------------------------------------------
# Installation and project setup


def skill_dirs() -> list[Path]:
    home = Path.home()
    # Explicit installation creates the canonical Codex location even on a
    # fresh machine. Claude remains opt-in through its existing config dir.
    candidates = [home / ".agents" / "skills"]
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    if claude.exists():
        candidates.append(claude / "skills")
    seen: set[Path] = set()
    result = []
    for path in candidates:
        real = path.resolve()
        if real not in seen:
            seen.add(real)
            result.append(path)
    return result


def link_dir(target: Path, link: Path) -> str:
    if link.is_symlink() or link.exists():
        if link.resolve() == target.resolve():
            return "present"
        raise Fail(f"{link} exists and does not point at {target}; remove it first")
    link.parent.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    return "linked"


SKILLS = ("enact", "enact-plan", "enact-setup", "enact-sweep")
# Skills Enact used to ship; install removes their now-dangling links.
RETIRED_SKILLS = ("enact-intake",)


def unlink_retired(target: Path, link: Path) -> bool:
    if link.exists() or Path(os.path.realpath(link)) != Path(os.path.realpath(target)):
        return False
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)  # a Windows junction
    return True


def cmd_install(args: argparse.Namespace) -> dict[str, Any]:
    linked = {}
    directories = skill_dirs()
    for directory in directories:
        for skill in SKILLS:
            link = directory / skill
            linked[str(link)] = link_dir(ENACT_ROOT / "skills" / skill, link)
        for skill in RETIRED_SKILLS:
            link = directory / skill
            if unlink_retired(ENACT_ROOT / "skills" / skill, link):
                linked[str(link)] = "removed"
    legacy = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills"
    # Migrate only links owned by this checkout. A shared Claude directory
    # must keep its links; never delete someone else's skill or directory.
    if legacy.resolve() not in {path.resolve() for path in directories}:
        for skill in (*SKILLS, *RETIRED_SKILLS):
            link = legacy / skill
            target = ENACT_ROOT / "skills" / skill
            try:
                info = link.lstat()
            except FileNotFoundError:
                continue
            junction = (WINDOWS and
                        info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT)
            if (link.is_symlink() or junction) and link.resolve() == target.resolve():
                if link.is_symlink():
                    link.unlink()
                else:
                    link.rmdir()
                linked[str(link)] = "removed"
    return {"skills": linked}


ENACT_TOML_TEMPLATE = """\
# Enact configuration. See {root}/README.md.
remote = "origin"
staging_branch = "staging"
master_branch = "{master}"

[checks]
fast = "{fast}"
slow = "{slow}"

[worktree]
root = ".worktrees"   # inside the checkout; gitignored
# create = "scripts/enact-worktree create"   # replaces `git worktree add`
# remove = "scripts/enact-worktree remove"   # replaces `git worktree remove`

# Model settings for spawned sessions, by role and by a bead's `tier:<name>`
# label (a tier overrides its role). The vizier labels mechanical beads
# `tier:light`; reviews and design work keep the default model. `tier:fable`,
# `tier:opus` and `tier:sonnet` are built in for Claude and need no entry
# here; map them for another provider with `[tiers.opus.<provider>]`.
[tiers.light.claude]
model = "sonnet"

# [roles.warden.claude]
# model = "opus"
# effort = "max"
"""


AUDIT_LOG = ".beads/interactions.jsonl"


def untrack_audit_log(repo: Repo) -> str:
    """Ignore bd's audit log and drop it from the index.

    bd appends to it in the main checkout, which Enact never commits to, so a
    tracked copy leaves the checkout dirty and blocks fast-forwards. The
    issue data itself lives in Dolt.
    """
    changes = []
    if repo.ignore(repo.main / ".gitignore", f"/{AUDIT_LOG}"):
        changes.append("ignored")
    if repo.git_ok("ls-files", "--error-unmatch", AUDIT_LOG, cwd=repo.main):
        repo.git("rm", "-q", "--cached", AUDIT_LOG, cwd=repo.main)
        changes.append("untracked")
    return " and ".join(changes) or "unchanged"


def cmd_setup(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    report: dict[str, Any] = {}
    for tool in ("git", "bd", "dolt"):
        report[f"has_{tool}"] = bool(shutil.which(tool))
    if not report["has_bd"]:
        raise Fail("bd (Beads) is not installed")
    toml = repo.main / ".enact.toml"
    if not toml.exists():
        toml.write_text(
            ENACT_TOML_TEMPLATE.format(
                root=ENACT_ROOT, master=args.master, fast=args.fast or "", slow=args.slow or ""
            ),
            encoding="utf-8",
            newline="\n",
        )
        report["enact_toml"] = "created"
    else:
        report["enact_toml"] = "present"
    repo = find_repo()
    beads = Beads(repo)
    if not (repo.main / ".beads").exists():
        # Enact's AGENTS.md section replaces bd's agent files and git hooks.
        cmd = ["init", "--non-interactive", "--skip-agents", "--skip-hooks"]
        if args.prefix:
            cmd += ["-p", args.prefix]
        beads.run(*cmd)
        report["beads"] = "initialized"
    elif beads.raw("list", "-n", "1", "--json").returncode != 0:
        beads.run("bootstrap")  # tracked .beads/ but no local database yet
        report["beads"] = "bootstrapped from remote"
    else:
        report["beads"] = "present"
    report["gitignore"] = repo.ignore_worktree_root(repo.main / ".gitignore") or "unchanged"
    report["audit_log"] = untrack_audit_log(repo)
    repo.git("config", "beads.role", "maintainer", cwd=repo.main, check=False)
    cfg = repo.config
    repo.git("fetch", "-q", cfg.remote, cwd=repo.main, check=False)
    if not repo.git_ok("rev-parse", "-q", "--verify", repo.remote_ref(cfg.staging), cwd=repo.main):
        base = repo.remote_ref(cfg.master)
        repo.git("push", "-q", cfg.remote, f"{base}:refs/heads/{cfg.staging}", cwd=repo.main)
        report["staging"] = f"created from {base}"
    else:
        report["staging"] = "present"
    report["local_branches"] = track_local_branches(repo)
    report["beads_remote"] = beads.has_remote()
    if report["beads_remote"]:
        report["sync"] = sync(repo)
    report["agents_template"] = str(ENACT_ROOT / "templates" / "AGENTS.md")
    return report


# ---------------------------------------------------------------------------
# Entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enact", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: Any, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    p = add("sync", cmd_sync, "Commit, merge and push the Beads database.")

    p = add("claim", cmd_claim, "Claim a bead fleet-wide (pull, claim, push).")
    p.add_argument("bead")
    p.add_argument("--actor")

    p = add("start", cmd_start, "Take over a bead reserved for this session by the vizier.")
    p.add_argument("bead")
    p.add_argument("--actor")
    p.add_argument("--reservation", help="token from the spawn prompt")

    p = add("close", cmd_close, "Close a bead, auto-close finished epics, sync.")
    p.add_argument("bead")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor")
    p.add_argument("--continue", dest="continue_lane", action="store_true",
                   help="then claim the next ready bead in this bead's lane")

    p = add("brief", cmd_brief, "Print one bead's working context: bead, epics, "
            "dependency handoffs and lane.")
    p.add_argument("bead")

    p = add("meta", cmd_meta, "Set metadata key=value pairs on a bead and sync.")
    p.add_argument("bead")
    p.add_argument("pairs", nargs="+")
    p.add_argument("--actor")
    p.add_argument("--expected-session", help="archive target session")
    p.add_argument("--reservation", help="token from the spawn action for routing changes")

    p = add("run", cmd_run, "Register, finish, pause, block, resume or prune shared runs.")
    p.add_argument("action", choices=["register", "finish", "pause", "block", "resume", "prune"])
    p.add_argument("epic", nargs="?")
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--authorized", action="store_true", help="retain invocation's chat authority")
    p.add_argument("--reason")
    p.add_argument("--explicit", action="store_true", help="user explicitly requested resume")
    p.add_argument("--exhausted", action="store_true", help="Justiciar exhausted recovery")

    p = add("next", cmd_next, "Vizier step: reserve the next bead to spawn, or wait.")
    p.add_argument("--epic", required=True)
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--vizier", help="vizier identity (default: this session)")
    p.add_argument("--wait", type=int, default=0, help="block up to N seconds for an action")
    p.add_argument("--interval", type=int, default=60)

    p = add("pool", cmd_pool, "Track the sessions a Claude Desktop vizier reuses.")
    p.add_argument("action", choices=["add", "drop", "rest", "status"])
    p.add_argument("sessions", nargs="*")
    p.add_argument("--all", action="store_true", help="drop every member")
    p.add_argument("--vizier", help="vizier identity (default: this session)")

    p = add("wait", cmd_wait, "Block until every named bead is closed.")
    p.add_argument("beads", nargs="+")
    p.add_argument("--timeout", type=int, default=540, help="seconds to block for")
    p.add_argument("--interval", type=int, default=30, help=argparse.SUPPRESS)

    p = add("push", cmd_push, "Integrate with staging, run fast checks, push, start slow CI.")
    p.add_argument("--bead", help="record the pushed commits on this bead")
    p.add_argument("--keep", action="store_true", help="keep the worktree after pushing")
    p.add_argument("--no-ci", action="store_true", help="do not start the slow-CI runner")
    p.add_argument("--skip-checks", action="store_true", help=argparse.SUPPRESS)

    p = add("ci", cmd_ci, "Run the coalescing slow-CI loop for this machine.")
    p.add_argument("--detach", action="store_true", help="start the runner in the background")
    p.add_argument("--status", action="store_true", help="report runner state")
    p.add_argument("--wait", metavar="SHA",
                   help="block until SHA reaches master or a Steward bead covers it")
    p.add_argument("--timeout", type=int, default=540, help="seconds --wait blocks for")
    p.add_argument("--interval", type=int, default=30, help=argparse.SUPPRESS)
    p.add_argument("--check-worker", help=argparse.SUPPRESS)
    p.add_argument("--coordinator", type=int, default=0, help=argparse.SUPPRESS)

    p = add("worktree", cmd_worktree,
            "Create or remove a bead worktree cut from staging, or prune dead ones.")
    p.add_argument("action", choices=["create", "remove", "prune"])
    p.add_argument("name", nargs="?")

    p = add("justiciar", cmd_justiciar, "Manage this machine's justiciar lock.")
    p.add_argument("action", choices=["status", "request", "acquire", "release", "break"])
    p.add_argument("--reason")
    p.add_argument("--actor")

    p = add("status", cmd_status, "Report this machine's Enact state.")

    p = add("release", cmd_release, "Return an abandoned bead to the queue.")
    p.add_argument("bead")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor")
    p.add_argument("--expected-owner", help="exact observed assignee")
    p.add_argument("--expected-session", help="exact observed child, or vizier if unspawned")
    p.add_argument("--reservation", help="token of the reservation being released")
    p.add_argument("--inactive", choices=["stopped", "archived", "missing"])
    p.add_argument("--evidence", help="successful provider lookup proving inactivity")

    p = add("install", cmd_install, "Link the Enact skills into user-level skill directories.")

    p = add("setup", cmd_setup, "Set up Enact in the current project.")
    p.add_argument("--prefix", help="Beads issue prefix")
    p.add_argument("--master", default="master")
    p.add_argument("--fast")
    p.add_argument("--slow")

    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("enact needs Python 3.11 or newer", file=sys.stderr)
        return 1
    args = build_parser().parse_args(argv)
    try:
        emit(args.func(args))
        return 0
    except Fail as error:
        emit({"error": str(error), **error.extra})
        log(str(error))
        return error.code


if __name__ == "__main__":
    sys.exit(main())
