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
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

ENACT_ROOT = Path(__file__).resolve().parent.parent
ROLES_DIR = ENACT_ROOT / "skills" / "enact" / "roles"
JUSTICIAR_REF = "refs/enact/justiciar"
EMOJI = {
    "vizier": "🔮",
    "executor": "⚒️",
    "warden": "🛡️",
    "justiciar": "🔥",
    "steward": "🚒",
    "marshal": "🎖️",
    "mason": "🧱",
    "sage": "📖",
    "archivist": "📜",
}
SPAWNED_ROLES = ("executor", "warden", "steward", "mason", "sage", "archivist", "justiciar")
ROLE_NOTICE = (
    "Enact roles are prompt files, not skills: never invoke a skill named after a role "
    f"({', '.join(EMOJI)}). Work fully unattended: nobody will answer questions."
)
WINDOWS = os.name == "nt"


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
    if check and proc.returncode != 0:
        raise Fail(
            f"{' '.join(cmd)} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[-2000:]}"
        )
    return proc


def run_shell(command: str, cwd: Path, env: dict[str, str] | None = None) -> int:
    """Run a project-configured shell command, streaming output to stderr."""
    log(f"$ {command}")
    proc = subprocess.run(
        command,
        shell=True,
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
        for name in (bead.get("assignee"), meta(bead, "enact_session")):
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


@dataclass(frozen=True)
class Config:
    remote: str = "origin"
    staging: str = "staging"
    master: str = "master"
    fast: str | None = None
    slow: str | None = None
    worktree_root: str = DEFAULT_WORKTREE_ROOT
    worktree_create: str | None = None
    worktree_remove: str | None = None
    roles: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def parse(text: str) -> Config:
        data = tomllib.loads(text)
        checks = data.get("checks", {})
        worktree = data.get("worktree", {})
        return Config(
            remote=data.get("remote", "origin"),
            staging=data.get("staging_branch", "staging"),
            master=data.get("master_branch", "master"),
            fast=checks.get("fast") or None,
            slow=checks.get("slow") or None,
            worktree_root=worktree.get("root", DEFAULT_WORKTREE_ROOT),
            worktree_create=worktree.get("create") or None,
            worktree_remove=worktree.get("remove") or None,
            roles=data.get("roles", {}),
        )

    def role_settings(self, role: str, host_provider: str) -> dict[str, str]:
        entry = self.roles.get(role, {}).get(host_provider, {})
        return {k: v for k, v in entry.items() if k in ("model", "effort") and v}


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
        ignore_file.write_text(text + pattern + "\n", encoding="utf-8")
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
        cmd = ["bd"]
        if actor:
            cmd += ["--actor", actor]
        return run([*cmd, *args], self.repo.main, check=False, env=self.env)

    def run(self, *args: str, actor: str | None = None) -> str:
        proc = self.raw(*args, actor=actor)
        if proc.returncode != 0:
            raise Fail(f"bd {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
        return proc.stdout

    def json(self, *args: str, actor: str | None = None) -> Any:
        out = self.run(*args, "--json", actor=actor)
        return json.loads(out) if out.strip() else None

    def show(self, bead_id: str) -> dict[str, Any]:
        data = self.json("show", bead_id)
        return data[0] if isinstance(data, list) else data

    def all(self) -> list[dict[str, Any]]:
        return self.json("list", "--all", "-n", "0") or []

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


def role_of(bead: dict[str, Any]) -> str:
    for label in bead.get("labels") or []:
        if label.startswith("role:"):
            return label.split(":", 1)[1]
    return "executor"


def targeted_here(bead: dict[str, Any]) -> bool:
    want_host = meta(bead, "enact_host")
    want_provider = meta(bead, "enact_provider")
    return (not want_host or want_host == host_name()) and (
        not want_provider or want_provider == provider()
    )


def dolt_db_dir(repo: Repo) -> Path:
    metadata = json.loads((repo.main / ".beads" / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("dolt_mode", "embedded") != "embedded":
        raise Fail("automatic Dolt conflict resolution needs embedded mode")
    return repo.main / ".beads" / "embeddeddolt" / metadata["dolt_database"]


def dolt_json(db: Path, query: str) -> list[dict[str, Any]]:
    out = run(["dolt", "sql", "-r", "json", "-q", query], db).stdout.strip()
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
    run(["dolt", "fetch", repo.config.remote], db)
    run(["dolt", "merge", f"{repo.config.remote}/{branch}"], db, check=False)
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
        run(["dolt", "conflicts", "resolve", "--theirs", table], db)
    run(["dolt", "add", "."], db)
    run(["dolt", "commit", "--allow-empty", "-m", "enact sync: merge remote (remote wins)"], db, check=False)
    for item in dropped:
        log(f"conflict: remote won for {item['table']} {item['id']}; dropped {item['dropped_local']}")
    return dropped


# Serialize the read/check/write/sync sequence within one clone. Remote
# conflicts still resolve remote-wins; verify the published owner afterwards.
_ownership_locks: set[Path] = set()


@contextlib.contextmanager
def ownership_lock(repo: Repo):
    path = repo.state / "ownership.lock"
    if path in _ownership_locks:
        yield
        return
    deadline = time.monotonic() + 180
    while (guard := try_lock(path)) is None:
        if time.monotonic() >= deadline:
            raise Fail("ownership operation busy; retry", code=3)
        time.sleep(0.1)
    _ownership_locks.add(path)
    try:
        yield
    finally:
        _ownership_locks.remove(path)
        release_lock(path, guard)


def serialized(function):
    @functools.wraps(function)
    def call(first, *args, **kwargs):
        repo = first if isinstance(first, Repo) else find_repo()
        with ownership_lock(repo):
            return function(first, *args, **kwargs)
    return call


@serialized
def sync(repo: Repo) -> dict[str, Any]:
    """Commit local Beads writes, merge the remote and push refs/dolt/data."""
    beads = Beads(repo)
    if not beads.has_remote():
        return {"remote": False, "dropped": []}
    dropped: list[dict[str, Any]] = []
    for attempt in range(8):
        beads.raw("dolt", "commit", "-m", "enact sync")
        pull = beads.raw("dolt", "pull")
        if pull.returncode != 0:
            text = (pull.stdout + pull.stderr).lower()
            if "no branches found" in text:
                pass  # first push of this database to the remote
            elif "conflict" not in text:
                raise Fail(f"bd dolt pull failed: {(pull.stderr or pull.stdout).strip()}")
            dropped += resolve_remote_wins(repo)
        push = beads.raw("dolt", "push")
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
    assigned = meta(bead, "enact_session")
    if bead.get("status") == "closed":
        raise Fail(f"{args.bead} is already closed", code=3)
    if owner != actor:
        if not owner.startswith("vizier:") or (assigned and assigned != actor):
            raise Fail(f"{args.bead} belongs to {owner}", code=3)
        # Legacy reservations have no token: only the recorded child may start.
        if (token and args.reservation != token) or (not token and assigned != actor):
            raise Fail("reservation does not match this worker", code=3)
    beads.run("update", args.bead, "--assignee", actor, "--status", "in_progress",
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
    beads.run("close", args.bead, "--reason", args.reason, actor=actor)
    result = sync(repo)
    saved = beads.show(args.bead)
    require_owner(saved, actor, closed=True)
    if saved.get("status") != "closed":
        raise Fail("close lost during sync; recheck ownership", code=3)
    closed = close_finished_epics(repo)
    return {"closed": args.bead, "epics_closed": closed, "sync": result}


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
                               "enact_worktree", "enact_branch", "enact_worktree_host"}
        if protected.intersection(pairs):
            raise Fail("ownership and recovery fields are managed by Enact", code=3)
    if any(key.startswith(("enact_control", "enact_vizier_route_",
                           "enact_marshal_attempt_")) for key in pairs):
        raise Fail("run state and recovery fields are managed by enact run", code=3)
    beads.set_meta(args.bead, pairs, actor=actor)
    result = sync(repo)
    saved = beads.show(args.bead)
    if any(meta(saved, key) != value for key, value in pairs.items()):
        raise Fail("metadata lost during sync; recheck ownership", code=3)
    return {"bead": args.bead, "metadata": pairs, "sync": result}


def cmd_sync(args: argparse.Namespace) -> dict[str, Any]:
    return sync(find_repo())


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


def run_delivery(repo: Repo, graph: list[dict[str, Any]], epic: str) -> dict[str, Any]:
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
            if sep and any(repo.git_ok("merge-base", "--is-ancestor", sha, tip)
                           and not repo.git_ok("merge-base", "--is-ancestor", sha, green)
                           for sha in commits):
                legacy.append(bead)
        if not legacy:
            break
        members += legacy
    master = repo.remote_ref(repo.config.master)
    pending = [sha for sha in commits if not repo.git_ok(
        "merge-base", "--is-ancestor", sha, master
    )]
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


def marshal_name(repo: Repo) -> str:
    identity = "|".join((str(repo.main.resolve()), host_name(), provider(),
                         os.environ.get("ENACT_APP", provider())))
    return "enact-marshal-" + hashlib.sha256(identity.encode()).hexdigest()[:20]


def route_key(session: str) -> str:
    return "enact_vizier_route_" + hashlib.sha256(session.encode()).hexdigest()[:24]


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
                    and all(c.get("status") == "closed" for c in children)]
        if not eligible:
            if closed:
                sync(repo)
            return closed
        for bead in eligible:
            beads.run("close", bead["id"], "--reason", "All children complete")
            bead["status"] = "closed"
            closed.append(bead["id"])


def recovery_budget(history: list[dict[str, Any]], epoch: str, limit: int,
                    timestamp: str) -> str | None:
    attempts = [item for item in history if item["epoch"] == epoch]
    if len(attempts) >= limit:
        return "exhausted"
    if attempts:
        elapsed = (dt.datetime.fromisoformat(timestamp) -
                   dt.datetime.fromisoformat(attempts[-1]["at"])).total_seconds()
        if elapsed < 3600:
            return "cooldown"
    return None


@serialized
def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    sync(repo)
    beads = Beads(repo)
    bead = beads.show(args.epic)
    if bead.get("issue_type") != "epic":
        raise Fail("run must name an epic")
    actor = require_actor(None)
    control = run_control(bead)
    if args.action == "register":
        return {"epic": args.epic, "control": control,
                "route": register_run(repo, args.epic, actor, args.parallel, args.authorized),
                "marshal_name": marshal_name(repo)}
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
    if args.kind != "notice":
        require_running(bead, beads.all())
    if not args.session or not args.evidence or not args.kind:
        raise Fail("recover requires --kind, --session and --evidence")
    route = next((r for r in vizier_routes(bead, beads.all())
                  if r.get("session") == args.session), {})
    limit = 1 if args.kind == "notice" else 3
    if args.kind == "worker":
        if not args.bead:
            raise Fail("worker recovery requires --bead")
        worker = beads.show(args.bead)
        require_running(worker, beads.all())
        if (meta(worker, "enact_session") != args.session or not routed_here(worker)
                or worker.get("status") == "closed" or worker.get("assignee") != args.session
                or args.epic not in bead_runs(worker, beads.all())):
            raise Fail("worker assignment or routing changed", code=3)
        route = json_meta(bead, route_key(meta(worker, "enact_vizier") or ""), {})
        limit = 2
    if ((args.kind != "notice" and not route.get("authorized"))
            or route.get("host") != host_name()
            or route.get("provider") != provider()
            or route.get("app") != os.environ.get("ENACT_APP", provider())
            or route.get("project") != str(repo.main)):
        raise Fail("recovery needs a registered, authorized route on this host/app", code=3)
    key = "enact_marshal_attempt_" + hashlib.sha256(
        f"{args.kind}:{args.session}".encode()).hexdigest()[:24]
    history = json_meta(bead, key, [])
    timestamp = now()
    denied = recovery_budget(history, control["epoch"], limit, timestamp)
    if denied:
        return {"allowed": False, "reason": denied, "history": history}
    attempt = {"id": uuid.uuid4().hex, "epoch": control["epoch"], "at": timestamp,
               "session": args.session, "kind": args.kind, "bead": args.bead,
               "evidence": args.evidence, "actor": actor}
    save_run_meta(repo, args.epic, key, history + [attempt])
    if args.kind != "notice":
        require_running(beads.show(args.epic), beads.all())
    return {"allowed": True, "attempt": attempt, "route": route,
            "remaining": limit - 1 - sum(item["epoch"] == control["epoch"] for item in history)}


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
        f"Start with `{start}`.\n"
        "The run authorizes creating, inspecting, messaging and archiving its "
        "role chats and managing its Marshal. Preserve this authorization "
        f"during recovery.\n\n{ROLE_NOTICE}"
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
    return {
        "action": "spawn",
        "bead": bead["id"],
        "role": role,
        "reservation": meta(bead, "enact_reservation"),
        "title": session_title(role, bead),
        "prompt": spawn_prompt(role, bead, epic, vizier),
        "settings": repo.config.role_settings(role, provider()),
    }


LOOP_REMINDER = (
    "Vizier loop: act on this result (archive listed sessions and record "
    "enact_archived=true; start one session per spawn action and record its "
    "id with `enact meta <bead> enact_session=<id> --reservation <token>` "
    "using that spawn action's reservation), then run `enact next` "
    "again with the same arguments. Never end your turn before action: done, paused or blocked, "
    "including when the epic is closed but delivery or Steward repairs are pending."
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
    result: dict[str, Any] = {
        "epic": epic,
        "vizier": vizier,
        "host": host_name(),
        "archive": [
            {"bead": b["id"], "session": meta(b, "enact_session")}
            for b in mine
            if b.get("status") == "closed"
            and meta(b, "enact_session")
            and meta(b, "enact_archived") != "true"
        ],
        "reminder": LOOP_REMINDER,
    }
    inflight = [b for b in mine if b.get("status") != "closed"]
    result["inflight"] = [b["id"] for b in inflight]

    delivery = run_delivery(repo, graph, epic)
    result["delivery"] = delivery
    if halted := stopped_runs(by_id[epic], graph):
        control = run_control(halted[0])
        return {**result, "action": control["state"], "control": control}

    request = justiciar_state(repo)
    if request and request.get("state") == "requested" and request.get("host") == host_name():
        result["justiciar"] = request
        if justiciar_transition(repo, request, {"state": "spawning", "vizier": vizier}):
            return {
                **result,
                "action": "spawn-justiciar",
                "title": f"{EMOJI['justiciar']} {request.get('reason', 'Emergency')[:48]}",
                "prompt": justiciar_prompt(request),
                "settings": repo.config.role_settings("justiciar", provider()),
            }

    if by_id[epic].get("status") == "closed" and not inflight and delivery["complete"]:
        return {**result, "action": "done"}
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
    ]
    candidates.sort(key=lambda b: (role_of(b) != "steward", b.get("priority", 2)))
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
        if claim(repo, bead["id"], f"vizier:{host_name()}:{provider()}:{vizier}", reservation):
            return {**result, **spawn_action(repo, beads.show(bead["id"]), epic, vizier)}
        log(f"lost the claim race for {bead['id']}; trying the next bead")
    if candidates:
        # The graph predates the winning claim; it cannot prove a deadlock.
        return {**result, "action": "wait", "reason": "claims changed"}

    open_scope = [by_id[i] for i in scope if by_id[i].get("status") != "closed"]
    working = [b for b in open_scope if b.get("status") == "in_progress"]
    elsewhere = [b for b in ready if b["id"] in scope and not targeted_here(b)]
    if not working and not inflight:
        held = [h for b in open_scope for h in stopped_runs(b, graph)]
        if held:
            control = run_control(held[0])
            return {**result, "action": control["state"], "control": control}
    if open_scope and not working and not elsewhere and not inflight:
        return {**result, "action": "deadlock", "open": [b["id"] for b in open_scope]}
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
    deadline = time.monotonic() + max(args.wait, 0)
    # Archives already reported (and possibly refused because the session was
    # still busy) only wake the vizier again at the end of the wait.
    reported_file = repo.state / f"archive-reported-{vizier.replace(':', '_')}.json"
    try:
        reported = set(json.loads(reported_file.read_text()))
    except (OSError, ValueError):
        reported = set()
    while True:
        sync(repo)
        result = schedule(repo, args.epic, args.parallel, vizier)
        pending = {item["bead"] for item in result["archive"]}
        timed_out = time.monotonic() >= deadline
        if result["action"] != "wait" or pending - reported or timed_out:
            reported_file.write_text(json.dumps(sorted(pending)))
            return result
        time.sleep(min(args.interval, max(deadline - time.monotonic(), 1)))


# ---------------------------------------------------------------------------
# Justiciar lock (a ref on the remote, independent of Beads)


def justiciar_state(repo: Repo) -> dict[str, Any] | None:
    remote = repo.config.remote
    listing = repo.git("ls-remote", remote, JUSTICIAR_REF, check=False)
    if not listing:
        return None
    sha = listing.split()[0]
    repo.git("fetch", "-q", remote, f"+{JUSTICIAR_REF}:{JUSTICIAR_REF}", check=False)
    body = repo.git("log", "-1", "--format=%B", sha, check=False)
    try:
        state = json.loads(body)
    except json.JSONDecodeError:
        state = {"state": "unknown", "raw": body}
    state["sha"] = sha
    return state


def justiciar_commit(repo: Repo, payload: dict[str, Any]) -> str:
    proc = subprocess.run(
        ["git", "mktree"], cwd=repo.top, input="", text=True, capture_output=True, check=True
    )
    empty_tree = proc.stdout.strip()
    message = json.dumps(payload, sort_keys=True)
    return repo.git("commit-tree", empty_tree, "-m", message)


def justiciar_push(repo: Repo, sha: str, expect: str) -> bool:
    lease = f"--force-with-lease={JUSTICIAR_REF}:{expect}"
    target = f"{sha}:{JUSTICIAR_REF}" if sha else f":{JUSTICIAR_REF}"
    return repo.git_ok("push", "-q", lease, repo.config.remote, target)


def justiciar_transition(repo: Repo, current: dict[str, Any], changes: dict[str, Any]) -> bool:
    payload = {k: v for k, v in current.items() if k != "sha"}
    payload.update(changes, updated=now())
    return justiciar_push(repo, justiciar_commit(repo, payload), current["sha"])


def justiciar_prompt(request: dict[str, Any]) -> str:
    script = Path(__file__).resolve()
    python = sys.executable or "python3"
    return (
        f"You are the Enact justiciar session (emoji {EMOJI['justiciar']}), requested by "
        f"{request.get('requester', 'unknown')} on host {request.get('host')}: "
        f"{request.get('reason', '')}\n\n"
        f"Read {ROLES_DIR / 'common.md'} and then {ROLES_DIR / 'justiciar.md'}, and follow "
        f"them. The Enact helper is `\"{python}\" \"{script}\"`, referred to as `enact`.\n\n"
        "The run authorizes creating, inspecting, messaging and archiving its "
        "role chats and managing its Marshal. Preserve this authorization "
        "during recovery.\n\n"
        f"{ROLE_NOTICE}"
    )


def cmd_justiciar(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    current = justiciar_state(repo)
    if args.action == "status":
        return {"justiciar": current}
    if args.action == "request":
        if current:
            raise Fail("a justiciar is already requested or active", code=3, holder=current)
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
            raise Fail("another justiciar is active", code=3, holder=current)
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
        if not justiciar_push(repo, "", current["sha"]):
            raise Fail("lock changed while releasing", code=3, holder=justiciar_state(repo))
        return {"released": current}
    raise Fail(f"unknown action {args.action}")


# ---------------------------------------------------------------------------
# Local branches that track the remote

TRACKED_OK = ("current", "fast-forwarded", "created")


def fetch(repo: Repo, check: bool = True) -> dict[str, str]:
    repo.git("fetch", "-q", repo.config.remote, cwd=repo.main, check=check)
    return track_local_branches(repo)


def checked_out_branches(repo: Repo) -> dict[str, Path]:
    """Map each branch ref checked out in a worktree to that worktree."""
    found: dict[str, Path] = {}
    path = None
    for line in repo.git("worktree", "list", "--porcelain", cwd=repo.main).splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line.startswith("branch ") and path:
            found[line[len("branch "):]] = path
    return found


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


def remove_worktree(repo: Repo, path: Path, branch: str | None) -> None:
    if repo.config.worktree_remove:
        env = worktree_env(repo, path, branch or "", "")
        if run_shell(repo.config.worktree_remove, repo.main, env) != 0:
            raise Fail("worktree remove hook failed")
        return
    repo.git("worktree", "remove", "--force", str(path), cwd=repo.main, check=False)
    if branch:
        repo.git("branch", "-D", branch, cwd=repo.main, check=False)
    repo.git("worktree", "prune", cwd=repo.main, check=False)


def worktree_bead(repo: Repo, path: Path) -> dict[str, Any] | None:
    # Includes legacy worktrees named after a bead, created before pointers
    # were recorded. Never let omitting --bead bypass ownership checks.
    for bead in Beads(repo).all():
        if path == repo.worktree_root() / bead["id"] or (
                meta(bead, "enact_worktree_host") == host_name() and
                meta(bead, "enact_worktree") == str(path)) or any(
                    r.get("host") == host_name() and r.get("path") == str(path)
                    for r in recovery_records(bead)):
            return bead
    return None


def verified_worktree(repo: Repo, path: Path, branch: str) -> None:
    if (not path.is_dir() or find_repo(path).common != repo.common or
            repo.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path) != branch):
        raise Fail(f"recovery worktree does not match recorded repository/branch: {path}", code=2)


@serialized
def cmd_worktree(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    path = repo.worktree_root() / args.name
    branch = f"enact/{args.name}"
    sync(repo)
    bead = worktree_bead(repo, path)
    actor = require_actor(None) if bead else None
    if bead:
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
            path = create_worktree(repo, args.name, branch, base)
        if bead:
            Beads(repo).set_meta(bead["id"], {
                "enact_worktree": str(path), "enact_branch": branch,
                "enact_worktree_host": host_name()}, actor=actor)
            sync(repo)
            require_owner(Beads(repo).show(bead["id"]), actor)
        return {"path": str(path), "branch": branch, "base": base, "recovered": reused}
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
    remove_worktree(repo, path, branch)
    return {"removed": str(path)}


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
def finish_delivery(repo: Repo, receipt: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Replay bookkeeping after a confirmed push; retain receipt on any failure."""
    result: dict[str, Any] = {"staging": data["head"], "commits": data["commits"]}
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


@serialized
def cmd_push(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
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
    for attempt in range(6):
        integrate(repo)
        before = repo.git("rev-parse", staging)
        head = repo.git("rev-parse", "HEAD")
        if head == before:
            if not recovered:
                return result
            result["recovered"] = recovered
            break
        if cfg.fast and not args.skip_checks:
            if run_shell(cfg.fast, repo.top) != 0:
                raise Fail("fast checks failed; fix them and run `enact push` again", code=2)
        commits = repo.git("rev-list", "--reverse", f"{before}..{head}").split()
        graph = Beads(repo).all() if args.bead else []
        bead = next((b for b in graph if b["id"] == args.bead), {})
        if args.bead and not bead:
            raise Fail(f"bead {args.bead} not found")
        data = {"bead": args.bead, "head": head, "commits": commits,
                "history": list(dict.fromkeys((meta(bead, "enact_commits") or "").split() + commits)),
                "runs": bead_runs(bead, graph), "ci": bool(cfg.slow and not args.no_ci)}
        receipt = delivery_receipts(repo) / f"{head}.json"
        # Write before the remote mutation, so a lost push response is recoverable.
        write_json(receipt, data)
        if args.bead:
            sync(repo)
            require_owner(Beads(repo).show(args.bead), require_actor(None))
        if repo.git_ok("push", "-q", cfg.remote, f"HEAD:refs/heads/{cfg.staging}"):
            result = {"pushed": True, **finish_delivery(repo, receipt, data)}
            if recovered:
                result["recovered"] = recovered
            break
        log(f"push to {cfg.staging} rejected (attempt {attempt + 1}); re-integrating")
        fetch(repo)
        recovered += recover_deliveries(repo)
    else:
        raise Fail(f"could not push to {cfg.staging} after repeated attempts")
    result["local_branches"] = track_local_branches(repo)
    branch = repo.git("rev-parse", "--abbrev-ref", "HEAD")
    in_enact_tree = (
        repo.top.resolve().parent == repo.worktree_root().resolve() and repo.top != repo.main
    )
    if in_enact_tree and not args.keep and not repo.git("status", "--porcelain"):
        os.chdir(repo.main)
        remove_worktree(repo, repo.top, branch if branch.startswith("enact/") else None)
        result["removed_worktree"] = str(repo.top)
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


def start_ci(repo: Repo) -> dict[str, Any]:
    status = ci_status(repo)
    if status["running"]:
        return {"started": False, "reason": "runner already active", **status}
    # The caller may be delivering Enact itself and remove its own worktree.
    # Keep an immutable copy in the git common directory before launching.
    source = Path(__file__).read_bytes()
    runner = repo.state / f"ci-runner-{hashlib.sha256(source).hexdigest()}.py"
    if not runner.exists():
        temp = runner.with_suffix(f".{os.getpid()}.tmp")
        temp.write_bytes(source)
        temp.replace(runner)
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
        beads.run("comments", "add", bead_id, body)
        # Preserve inferred legacy associations before replacing the old range.
        runs += [b["id"] for b in graph if b.get("issue_type") == "epic"
                 and bead_id in run_delivery(repo, graph, b["id"])["stewards"]]
        runs = list(dict.fromkeys((meta(open_stewards[0], "enact_runs") or "").split() + runs))
        beads.set_meta(bead_id, {"enact_runs": " ".join(runs),
                                 "enact_ci_range": f"{green}..{tip}"})
    else:
        created = beads.json(
            "create",
            "--title", f"Fix slow CI failure on {os_name}",
            "--description", body,
            "--acceptance", f"`{repo.config.slow}` passes on {os_name} at the staging tip.",
            "--priority", "0",
            "--labels", "role:steward",
            "--metadata", json.dumps({"enact_ci_os": os_name, "enact_ci_range": f"{green}..{tip}",
                                     "enact_runs": " ".join(runs)}),
        )
        bead_id = created["id"]
    sync(repo)
    return bead_id


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
                result = ci_supervise_check(repo, tip)
                results.append(result)
        finally:
            release_lock(lock, guard)
        fetch(repo)
        tip = repo.git("rev-parse", repo.remote_ref(cfg.staging), cwd=repo.main)
        if tip == (last_file.read_text().strip() if last_file.exists() else ""):
            return {"ran": results}


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
        # Restore before exiting, so legacy contenders never see a dead
        # keeper while the live coordinator still owns the OS guard.
        # If the coordinator died, our stale PID is recoverable after exit.
        if pid_alive(coordinator):
            write_json(lock, coordinator)


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
    log(f"slow CI on {tip[:12]} in {path}")
    try:
        proc = subprocess.run(
            cfg.slow, shell=True, cwd=path, text=True, encoding="utf-8", errors="replace",
            capture_output=True, stdin=subprocess.DEVNULL,
        )
    finally:
        remove_worktree(repo, path, None)
    output = (proc.stdout or "") + (proc.stderr or "")
    sys.stderr.write(output[-20000:])
    if proc.returncode == 0:
        write_json(promotion, {"tip": tip})
        advanced = ci_pass(repo, tip)
        if advanced:
            promotion.unlink(missing_ok=True)
        log(f"slow CI passed at {tip[:12]}; master advanced: {advanced}")
        return {"tip": tip, "passed": True, "master_advanced": advanced}
    tail = "\n".join(output.strip().splitlines()[-60:])
    bead_id = report_ci_failure(repo, tip, green, tail)
    log(f"slow CI failed at {tip[:12]}; steward bead {bead_id}")
    return {"tip": tip, "passed": False, "steward": bead_id}


# ---------------------------------------------------------------------------
# Marshal support


def spawned_here(bead: dict[str, Any], host: str, host_provider: str) -> bool:
    """Spawned by a vizier in this host app on this machine. Beads reserved
    before provider/app routing existed remain visible for inspection."""
    spawn_provider = meta(bead, "enact_spawn_provider")
    spawn_app = meta(bead, "enact_spawn_app")
    return meta(bead, "enact_spawn_host") == host and (
        not spawn_provider or spawn_provider == host_provider
    ) and (not spawn_app or spawn_app == os.environ.get("ENACT_APP", host_provider))


def vizier_routes(bead: dict[str, Any], graph: list[dict[str, Any]]) -> list[dict[str, Any]]:
    routes = [json_meta(bead, key, {}) for key in (bead.get("metadata") or {})
              if key.startswith("enact_vizier_route_")]
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
    """Everything the Marshal needs for its sweep of this machine and host
    app: a Claude Marshal cannot see Codex threads, and vice versa."""
    repo = find_repo()
    sync(repo)
    local_branches = fetch(repo, check=False)
    recover_deliveries(repo)
    graph = Beads(repo).all()
    here = host_name()
    ours = [b for b in graph if spawned_here(b, here, provider())]
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
        and meta(b, "enact_archived") != "true"
    ]
    deliveries = {b["id"]: run_delivery(repo, graph, b["id"])
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
        if bead.get("status") == "closed" and delivery["complete"]:
            continue
        halted = stopped_runs(bead, graph)
        control = run_control(halted[0] if halted else bead)
        active_runs.append({"epic": bead["id"], "control": control, "routes": routes,
                            "delivery": delivery, "recoverable": not halted,
                            "recovery": {key: json_meta(bead, key, [])
                                         for key in (bead.get("metadata") or {})
                                         if key.startswith("enact_marshal_attempt_")}})
    pending_deliveries = {epic: delivery for epic, delivery in deliveries.items()
                          if not delivery["complete"]}
    staging = repo.remote_ref(repo.config.staging)
    master = repo.remote_ref(repo.config.master)
    lag = repo.git("rev-list", "--count", f"{master}..{staging}", check=False)
    return {
        "host": here,
        "provider": provider(),
        "working": working,
        "active_runs": active_runs,
        "marshal_name": marshal_name(repo),
        "sweep_needed": bool(working or unarchived or any(
            r["recoverable"] or not r["delivery"]["complete"] for r in active_runs)),
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
    beads.run(
        "update", args.bead, "--assignee", "", "--status", "open",
        "--set-metadata", "enact_recovery=" + json.dumps(records),
        "--unset-metadata", "enact_vizier", "--unset-metadata", "enact_session",
        "--unset-metadata", "enact_actor", "--unset-metadata", "enact_archived",
        "--unset-metadata", "enact_reservation", actor=actor)
    result = sync(repo)
    saved = beads.show(args.bead)
    if saved.get("assignee") or recovery_records(saved) != records:
        raise Fail("release lost during sync; recheck ownership", code=3)
    beads.run("comments", "add", args.bead, f"Released by Enact: {args.reason}", actor=actor)
    sync(repo)
    return {"released": args.bead, "recovery": record, "sync": result}


def cmd_lock(args: argparse.Namespace) -> dict[str, Any]:
    """Machine-local named lock for agent sweeps (the Marshal)."""
    repo = find_repo()
    path = repo.state / f"{args.name}.lock"
    if args.action == "release":
        path.unlink(missing_ok=True)
        return {"released": args.name}
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < args.stale_minutes * 60:
            raise Fail(f"{args.name} lock held for {age / 60:.0f} minutes", code=3)
        log(f"breaking stale {args.name} lock ({age / 60:.0f} minutes old)")
    path.write_text(json.dumps({"session": session_actor(), "since": now()}))
    return {"acquired": args.name}


# ---------------------------------------------------------------------------
# Installation and project setup


def skill_dirs() -> list[Path]:
    home = Path.home()
    candidates = [
        Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "skills",
        Path(os.environ.get("CODEX_HOME", home / ".codex")) / "skills",
        home / ".agents" / "skills",
    ]
    seen: set[Path] = set()
    result = []
    for path in candidates:
        if not path.parent.exists():
            continue
        real = path.resolve() if path.exists() else path
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
    for directory in skill_dirs():
        for skill in ("enact", "enact-setup"):
            link = directory / skill
            linked[str(link)] = link_dir(ENACT_ROOT / "skills" / skill, link)
        for skill in RETIRED_SKILLS:
            link = directory / skill
            if unlink_retired(ENACT_ROOT / "skills" / skill, link):
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

    p = add("meta", cmd_meta, "Set metadata key=value pairs on a bead and sync.")
    p.add_argument("bead")
    p.add_argument("pairs", nargs="+")
    p.add_argument("--actor")
    p.add_argument("--expected-session", help="Marshal archive target")
    p.add_argument("--reservation", help="token from the spawn action for routing changes")

    p = add("run", cmd_run, "Register, pause, block, explicitly resume or recover a shared run.")
    p.add_argument("action", choices=["register", "pause", "block", "resume", "recover"])
    p.add_argument("epic")
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--authorized", action="store_true", help="retain invocation's chat/Marshal authority")
    p.add_argument("--reason")
    p.add_argument("--explicit", action="store_true", help="user explicitly requested resume")
    p.add_argument("--exhausted", action="store_true", help="Justiciar exhausted recovery")
    p.add_argument("--kind", choices=["worker", "vizier", "notice"])
    p.add_argument("--session")
    p.add_argument("--bead")
    p.add_argument("--evidence")

    p = add("next", cmd_next, "Vizier step: reserve the next bead to spawn, or wait.")
    p.add_argument("--epic", required=True)
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--vizier", help="vizier identity (default: this session)")
    p.add_argument("--wait", type=int, default=0, help="block up to N seconds for an action")
    p.add_argument("--interval", type=int, default=60)

    p = add("push", cmd_push, "Integrate with staging, run fast checks, push, start slow CI.")
    p.add_argument("--bead", help="record the pushed commits on this bead")
    p.add_argument("--keep", action="store_true", help="keep the worktree after pushing")
    p.add_argument("--no-ci", action="store_true", help="do not start the slow-CI runner")
    p.add_argument("--skip-checks", action="store_true", help=argparse.SUPPRESS)

    p = add("ci", cmd_ci, "Run the coalescing slow-CI loop for this machine.")
    p.add_argument("--detach", action="store_true", help="start the runner in the background")
    p.add_argument("--status", action="store_true", help="report runner state")
    p.add_argument("--check-worker", help=argparse.SUPPRESS)
    p.add_argument("--coordinator", type=int, default=0, help=argparse.SUPPRESS)

    p = add("worktree", cmd_worktree, "Create or remove a bead worktree cut from staging.")
    p.add_argument("action", choices=["create", "remove"])
    p.add_argument("name")

    p = add("justiciar", cmd_justiciar, "Manage the fleet-wide justiciar lock.")
    p.add_argument("action", choices=["status", "request", "acquire", "release", "break"])
    p.add_argument("--reason")
    p.add_argument("--actor")

    p = add("status", cmd_status, "Report this machine's Enact state (for the Marshal).")

    p = add("release", cmd_release, "Return an abandoned bead to the queue.")
    p.add_argument("bead")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor")
    p.add_argument("--expected-owner", help="exact observed assignee")
    p.add_argument("--expected-session", help="exact observed child, or vizier if unspawned")
    p.add_argument("--reservation", help="token of the reservation being released")
    p.add_argument("--inactive", choices=["stopped", "archived", "missing"])
    p.add_argument("--evidence", help="successful provider lookup proving inactivity")

    p = add("lock", cmd_lock, "Acquire or release a machine-local named lock.")
    p.add_argument("action", choices=["acquire", "release"])
    p.add_argument("name")
    p.add_argument("--stale-minutes", type=int, default=120)

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
