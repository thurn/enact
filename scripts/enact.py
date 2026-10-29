#!/usr/bin/env python3
"""Enact helper: git and Beads plumbing for the Enact orchestration framework.

Stdlib only, Python 3.11+, macOS, Linux and Windows. Every subcommand prints
exactly one JSON object on stdout; progress and diagnostics go to stderr.

Exit codes: 0 success, 1 failure, 2 the agent must act (for example resolve a
rebase conflict), 3 lost a race (claim or lock held by someone else).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def claim(repo: Repo, bead_id: str, actor: str, extra_meta: dict[str, str]) -> bool:
    """Claim a bead fleet-wide. Returns False when another actor won."""
    beads = Beads(repo)
    sync(repo)
    bead = beads.show(bead_id)
    if bead.get("status") == "closed":
        return False
    if bead.get("assignee") and bead["assignee"] != actor:
        return False
    args = ["update", bead_id, "--claim"]
    for key, value in extra_meta.items():
        args += ["--set-metadata", f"{key}={value}"]
    beads.run(*args, actor=actor)
    sync(repo)
    return beads.show(bead_id).get("assignee") == actor


def cmd_claim(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    actor = require_actor(args.actor)
    if not claim(repo, args.bead, actor, {}):
        raise Fail(f"{args.bead} is claimed by someone else or closed", code=3)
    return {"claimed": args.bead, "assignee": actor}


def cmd_start(args: argparse.Namespace) -> dict[str, Any]:
    """A spawned session takes its bead over from the vizier's reservation."""
    repo = find_repo()
    actor = require_actor(args.actor)
    beads = Beads(repo)
    sync(repo)
    bead = beads.show(args.bead)
    owner = bead.get("assignee") or ""
    if bead.get("status") == "closed":
        raise Fail(f"{args.bead} is already closed", code=3)
    if owner and owner != actor and not owner.startswith("vizier:"):
        raise Fail(f"{args.bead} belongs to {owner}", code=3)
    beads.run(
        "update",
        args.bead,
        "--assignee",
        actor,
        "--status",
        "in_progress",
        "--set-metadata",
        f"enact_actor={actor}",
        actor=actor,
    )
    sync(repo)
    bead = beads.show(args.bead)
    if bead.get("assignee") != actor:
        raise Fail(f"lost {args.bead} to {bead.get('assignee')}", code=3)
    return {"bead": bead, "role": role_of(bead), "emoji": EMOJI.get(role_of(bead), "")}


def cmd_close(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    actor = require_actor(args.actor)
    beads = Beads(repo)
    beads.run("close", args.bead, "--reason", args.reason, actor=actor)
    closed = beads.json("epic", "close-eligible") or {}
    result = sync(repo)
    return {"closed": args.bead, "epics_closed": closed.get("closed", []), "sync": result}


def cmd_meta(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    pairs = {}
    for item in args.pairs:
        key, sep, value = item.partition("=")
        if not sep:
            raise Fail(f"expected key=value, got {item!r}")
        pairs[key] = value
    Beads(repo).set_meta(args.bead, pairs, actor=args.actor or session_actor())
    return {"bead": args.bead, "metadata": pairs, "sync": sync(repo)}


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


def spawn_prompt(role: str, bead: dict[str, Any], epic: str, vizier: str) -> str:
    script = Path(__file__).resolve()
    python = sys.executable or "python3"
    return (
        f"You are an Enact {role} session (emoji {EMOJI.get(role, '')}). "
        f"Your bead is {bead['id']} ({bead.get('title', '')}) in epic {epic}; "
        f"you were started by vizier {vizier} on host {host_name()}.\n\n"
        f"Read {ROLES_DIR / 'common.md'} and then {ROLES_DIR / (role + '.md')}, "
        f"and follow them. The Enact helper is `\"{python}\" \"{script}\"`, "
        f"referred to as `enact` in those files.\n\n{ROLE_NOTICE}"
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
        "title": session_title(role, bead),
        "prompt": spawn_prompt(role, bead, epic, vizier),
        "settings": repo.config.role_settings(role, provider()),
    }


LOOP_REMINDER = (
    "Vizier loop: act on this result (archive listed sessions and record "
    "enact_archived=true; start one session per spawn action and record its "
    "id with `enact meta <bead> enact_session=<id>`), then run `enact next` "
    "again with the same arguments. Never end your turn while the epic is open."
)


def schedule(repo: Repo, epic: str, parallel: int, vizier: str) -> dict[str, Any]:
    beads = Beads(repo)
    beads.raw("epic", "close-eligible")
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

    if by_id[epic].get("status") == "closed" and not inflight:
        return {**result, "action": "done"}
    if len(inflight) >= parallel:
        return {**result, "action": "wait", "reason": "capacity"}

    scope = {b["id"] for b in descendants(graph, epic)}
    # The Archivist documents the shipped state, so it starts only once it is
    # the last open work in the epic, including follow-ups filed mid-run.
    archivist_waits = any(
        by_id[i].get("status") != "closed"
        and by_id[i].get("issue_type") != "epic"
        and role_of(by_id[i]) != "archivist"
        for i in scope
    )
    ready = beads.json("ready", "-n", "0", "--exclude-type", "epic", "--unassigned") or []
    candidates = [
        b
        for b in ready
        if (b["id"] in scope or (role_of(b) == "steward" and not b.get("parent")))
        and targeted_here(b)
        and not (archivist_waits and role_of(b) == "archivist")
    ]
    candidates.sort(key=lambda b: (role_of(b) != "steward", b.get("priority", 2)))
    for bead in candidates:
        reservation = {"enact_vizier": vizier, "enact_spawn_host": host_name()}
        if claim(repo, bead["id"], f"vizier:{host_name()}", reservation):
            return {**result, **spawn_action(repo, by_id.get(bead["id"], bead), epic, vizier)}
        log(f"lost the claim race for {bead['id']}; trying the next bead")

    open_scope = [by_id[i] for i in scope if by_id[i].get("status") != "closed"]
    working = [b for b in open_scope if b.get("status") == "in_progress"]
    elsewhere = [b for b in ready if b["id"] in scope and not targeted_here(b)]
    if open_scope and not working and not elsewhere and not inflight:
        return {**result, "action": "deadlock", "open": [b["id"] for b in open_scope]}
    stale = stale_hours(open_scope)
    if open_scope and not inflight and stale is not None and stale >= 6:
        return {**result, "action": "deadlock", "reason": f"no change for {stale:.1f}h"}
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


def cmd_worktree(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    branch = f"enact/{args.name}"
    if args.action == "create":
        fetch(repo)
        base = repo.git("rev-parse", repo.remote_ref(repo.config.staging), cwd=repo.main)
        path = create_worktree(repo, args.name, branch, base)
        return {"path": str(path), "branch": branch, "base": base}
    path = repo.worktree_root() / args.name
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


def cmd_push(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    cfg = repo.config
    if repo.git("status", "--porcelain", "--untracked-files=no"):
        raise Fail("working tree has uncommitted changes; commit them first", code=2)
    staging = repo.remote_ref(cfg.staging)
    for attempt in range(6):
        integrate(repo)
        before = repo.git("rev-parse", staging)
        head = repo.git("rev-parse", "HEAD")
        if head == before:
            return {"pushed": False, "reason": "nothing to push"}
        if cfg.fast and not args.skip_checks:
            if run_shell(cfg.fast, repo.top) != 0:
                raise Fail("fast checks failed; fix them and run `enact push` again", code=2)
        if repo.git_ok("push", "-q", cfg.remote, f"HEAD:refs/heads/{cfg.staging}"):
            break
        log(f"push to {cfg.staging} rejected (attempt {attempt + 1}); re-integrating")
    else:
        raise Fail(f"could not push to {cfg.staging} after repeated attempts")
    commits = repo.git("rev-list", "--reverse", f"{before}..{head}").split()
    result: dict[str, Any] = {"pushed": True, "staging": head, "commits": commits}
    result["local_branches"] = track_local_branches(repo)
    if args.bead:
        beads = Beads(repo)
        beads.set_meta(args.bead, {"enact_commits": " ".join(commits)}, actor=session_actor())
        beads.run("comments", "add", args.bead,
                  f"Pushed to {cfg.staging}: {before[:10]}..{head[:10]}", actor=session_actor())
        result["sync"] = sync(repo)
    if cfg.slow and not args.no_ci:
        result["ci"] = start_ci(repo)
    branch = repo.git("rev-parse", "--abbrev-ref", "HEAD")
    in_enact_tree = (
        repo.top.resolve().parent == repo.worktree_root().resolve() and repo.top != repo.main
    )
    if in_enact_tree and not args.keep:
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


def try_lock(path: Path) -> bool:
    """Create a PID lockfile atomically, replacing it if its owner is dead."""
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(path.read_text().strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid_alive(pid):
                return False
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return True
    return False


def ci_status(repo: Repo) -> dict[str, Any]:
    lock = repo.state / "ci.lock"
    pid = 0
    if lock.exists():
        try:
            pid = int(lock.read_text().strip() or "0")
        except ValueError:
            pid = 0
    last = repo.state / "ci-last"
    return {
        "running": pid_alive(pid),
        "pid": pid or None,
        "last_tested": last.read_text().strip() if last.exists() else None,
        "log": str(repo.state / "ci.log"),
    }


def start_ci(repo: Repo) -> dict[str, Any]:
    status = ci_status(repo)
    if status["running"]:
        return {"started": False, "reason": "runner already active", **status}
    log_file = open(repo.state / "ci.log", "a", encoding="utf-8")
    cmd = [sys.executable, str(Path(__file__).resolve()), "ci"]
    kwargs: dict[str, Any] = {}
    if WINDOWS:
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
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
    open_stewards = [
        b
        for b in beads.json("list", "--label", "role:steward", "-n", "0") or []
        if b.get("status") != "closed" and meta(b, "enact_ci_os") == os_name
    ]
    body = (
        f"Slow CI (`{repo.config.slow}`) failed on {host_name()} ({os_name}) at "
        f"{tip}. Untested range since last green master: {green}..{tip}.\n\n"
        f"Log tail:\n```\n{log_tail}\n```"
    )
    if open_stewards:
        bead_id = open_stewards[0]["id"]
        beads.run("comments", "add", bead_id, body)
    else:
        created = beads.json(
            "create",
            "--title", f"Fix slow CI failure on {os_name}",
            "--description", body,
            "--acceptance", f"`{repo.config.slow}` passes on {os_name} at the staging tip.",
            "--priority", "0",
            "--labels", "role:steward",
            "--metadata", json.dumps({"enact_ci_os": os_name, "enact_ci_range": f"{green}..{tip}"}),
        )
        bead_id = created["id"]
    sync(repo)
    return bead_id


def ci_pass(repo: Repo, tip: str) -> bool:
    cfg = repo.config
    master = repo.remote_ref(cfg.master)
    if repo.git_ok("rev-parse", "-q", "--verify", master) and not repo.git_ok(
        "merge-base", "--is-ancestor", master, tip
    ):
        log(f"{master} is not an ancestor of {tip}; leaving master alone")
        return False
    if not repo.git_ok("push", "-q", cfg.remote, f"{tip}:refs/heads/{cfg.master}"):
        return False
    track_local_branches(repo)
    return True


def cmd_ci(args: argparse.Namespace) -> dict[str, Any]:
    repo = find_repo()
    cfg = repo.config
    if args.detach:
        return start_ci(repo)
    if args.status:
        return ci_status(repo)
    if not cfg.slow:
        raise Fail("no [checks] slow command in .enact.toml")
    lock = repo.state / "ci.lock"
    last_file = repo.state / "ci-last"
    results: list[dict[str, Any]] = []
    while True:
        if not try_lock(lock):
            return {"ran": results, "reason": "another runner holds the lock"}
        try:
            while True:
                fetch(repo)
                tip = repo.git("rev-parse", repo.remote_ref(cfg.staging), cwd=repo.main)
                last = last_file.read_text().strip() if last_file.exists() else ""
                if tip == last:
                    break
                results.append(ci_run_once(repo, tip))
                last_file.write_text(tip)
        finally:
            lock.unlink(missing_ok=True)
        fetch(repo)
        tip = repo.git("rev-parse", repo.remote_ref(cfg.staging), cwd=repo.main)
        if tip == (last_file.read_text().strip() if last_file.exists() else ""):
            return {"ran": results}


def ci_run_once(repo: Repo, tip: str) -> dict[str, Any]:
    cfg = repo.config
    green = repo.git("rev-parse", repo.remote_ref(cfg.master), cwd=repo.main, check=False) or "?"
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
        advanced = ci_pass(repo, tip)
        log(f"slow CI passed at {tip[:12]}; master advanced: {advanced}")
        return {"tip": tip, "passed": True, "master_advanced": advanced}
    tail = "\n".join(output.strip().splitlines()[-60:])
    bead_id = report_ci_failure(repo, tip, green, tail)
    log(f"slow CI failed at {tip[:12]}; steward bead {bead_id}")
    return {"tip": tip, "passed": False, "steward": bead_id}


# ---------------------------------------------------------------------------
# Marshal support


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    """Everything the Marshal needs for its sweep of this machine."""
    repo = find_repo()
    sync(repo)
    local_branches = fetch(repo, check=False)
    graph = Beads(repo).all()
    here = host_name()
    working = [
        {
            "bead": b["id"],
            "title": b.get("title"),
            "assignee": b.get("assignee"),
            "status": b.get("status"),
            "session": meta(b, "enact_session"),
            "vizier": meta(b, "enact_vizier"),
            "updated_at": b.get("updated_at"),
        }
        for b in graph
        if meta(b, "enact_spawn_host") == here and b.get("status") != "closed"
    ]
    unarchived = [
        {"bead": b["id"], "session": meta(b, "enact_session")}
        for b in graph
        if meta(b, "enact_spawn_host") == here
        and b.get("status") == "closed"
        and meta(b, "enact_session")
        and meta(b, "enact_archived") != "true"
    ]
    staging = repo.remote_ref(repo.config.staging)
    master = repo.remote_ref(repo.config.master)
    lag = repo.git("rev-list", "--count", f"{master}..{staging}", check=False)
    return {
        "host": here,
        "working": working,
        "unarchived": unarchived,
        "ci": ci_status(repo),
        "master_lag": int(lag) if lag.isdigit() else None,
        "local_branches": local_branches,
        "justiciar": justiciar_state(repo),
        "open_stewards": [
            b["id"] for b in graph if role_of(b) == "steward" and b.get("status") != "closed"
        ],
    }


def cmd_release(args: argparse.Namespace) -> dict[str, Any]:
    """Return an abandoned bead to the queue (its session is gone)."""
    repo = find_repo()
    beads = Beads(repo)
    beads.run(
        "update", args.bead, "--assignee", "", "--status", "open",
        "--unset-metadata", "enact_vizier", "--unset-metadata", "enact_session",
        "--unset-metadata", "enact_actor", "--unset-metadata", "enact_archived",
        actor=args.actor or session_actor(),
    )
    beads.run("comments", "add", args.bead, f"Released by Enact: {args.reason}",
              actor=args.actor or session_actor())
    return {"released": args.bead, "sync": sync(repo)}


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

    p = add("close", cmd_close, "Close a bead, auto-close finished epics, sync.")
    p.add_argument("bead")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor")

    p = add("meta", cmd_meta, "Set metadata key=value pairs on a bead and sync.")
    p.add_argument("bead")
    p.add_argument("pairs", nargs="+")
    p.add_argument("--actor")

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
