"""Exercise the fleet switch with real Beads, Dolt Git remotes, and Wrangler."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import gzip
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent / "scripts"
SPEC = spec_from_file_location("shim_replay", HERE / "shim-replay.py")
shim = module_from_spec(SPEC)
SPEC.loader.exec_module(shim)

MACHINES = ("first-mac", "second-mac", "windows")
WRITER = """
use std::{env, fs, path::PathBuf, process::{Command, exit}, thread, time::{Duration, Instant}};
fn main() {
    let args: Vec<_> = env::args_os().skip(1).collect();
    if env::var_os("CUTOVER_WRITER").is_some() {
        let gate = PathBuf::from(env::var_os("CUTOVER_WRITER").unwrap());
        fs::write(gate.join("entered"), "").unwrap();
        let deadline = Instant::now() + Duration::from_secs(120);
        while !gate.join("release").exists() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(20));
        }
    }
    let result = Command::new(env::var_os("CUTOVER_LEGACY").unwrap()).args(args).status().unwrap();
    exit(result.code().unwrap_or(1));
}
"""


def checked(binary, args, directory, env):
    result = shim.run(binary, args, directory, env)
    assert result.returncode == 0, (args, result)
    return result.stdout


@contextmanager
def running(binary, args, directory, env):
    with subprocess.Popen([binary, *args], cwd=directory, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()


def finish(process, expected=0):
    out, error = process.communicate(timeout=120)
    assert process.returncode == expected, (process.args, out, error)
    return out, error


def wait_for(condition, process):
    deadline = time.monotonic() + 120
    while not condition():
        if process.poll() is not None:
            raise AssertionError(process.communicate())
        assert time.monotonic() < deadline, process.args
        time.sleep(0.05)


def prepare_replay(directory):
    # The command samples and initial export are real recorded fixtures. Use
    # a current fixture timestamp to exercise the operator's freshness check.
    corpus = directory / "corpus"
    corpus.mkdir()
    rows = shim.read_gzip(shim.CORPUS / "start.jsonl.gz")
    row = next(row for row in rows if row["id"] == "by-nf2q")
    with gzip.open(corpus / "start.jsonl.gz", "wt", encoding="utf-8") as output:
        output.write(json.dumps(row) + "\n")
    calls = shim.read_gzip(shim.CORPUS / "calls.jsonl.gz")
    with gzip.open(corpus / "calls.jsonl.gz", "wt", encoding="utf-8") as output:
        first_creates = calls[1:7]
        exports = (call for call in calls if call["command"] == ["export"])
        for call in (*first_creates, *exports):
            output.write(json.dumps({**call, "start_ns": time.time_ns()}) + "\n")
    return corpus


def fixture(tk, legacy, directory, project, address, env):
    source = directory / "source"
    source.mkdir(parents=True)
    checked("git", ["init", "-q", "-b", "master"], source, env)
    checked("git", ["commit", "-qm", "Initial commit", "--allow-empty"], source, env)
    origin = directory / "origin.git"
    checked("git", ["clone", "-q", "--bare", str(source), str(origin)], directory, env)
    checked(legacy, ["init", "--prefix", project, "--skip-agents", "--skip-hooks",
                     "--non-interactive"], source, env)
    checked("git", ["remote", "add", "origin", origin.as_uri()], source, env)
    database = source / ".beads" / "embeddeddolt" / project
    for field, value in (("user.name", "Rehearsal"), ("user.email", "rehearsal@example.com")):
        checked("dolt", ["config", "--local", "--add", field, value], database, env)
    checked("dolt", ["remote", "add", "origin", f"git+{origin.as_uri()}"], database, env)
    tickets = [json.loads(checked(legacy, ["--sandbox", "create", "--title", f"Fixture {n}", "--json"],
                                  source, env))["id"] for n in range(4)]
    checked(legacy, ["--sandbox", "close", tickets[-1], "--reason", "Complete"], source, env)
    checked("dolt", ["push", "origin", "main"], database, env)
    (source / ".enact.toml").write_text(f'[tickets]\nproject = "{project}"\nurl = "{address}"\n')
    environments = []
    for machine in MACHINES:
        home = directory / machine
        tokens = home / ("enact" if os.name == "nt" else ".config/enact")
        tokens.mkdir(parents=True)
        (tokens / "token").write_text(machine)
        shim.request(address, f"/test-token/{project}?machine={machine}", token=machine)
        worker = {**env, "HOME": str(home), "APPDATA": str(home), "USERPROFILE": str(home)}
        shim.ok(tk, ["--json", "event", "machine", '{"enrolled":true}'], source, worker)
        environments.append(worker)
    return source, database, tickets, environments


def native(tk, source, env, *args):
    return shim.ok(tk, ["--json", *args], source, env)


def exercise(tk, legacy, bd, directory, project, address, env, corpus, scenario):
    source, database, tickets, machines = fixture(tk, legacy, directory, project, address, env)
    operator = machines[0]
    command = [str(SCRIPTS / "cutover.py")]
    if scenario == "success":
        budget = native(tk, source, operator, "state")["rows_written"] + 12
        partial = shim.run(sys.executable, [*command, "preimport", "--row-budget", str(budget)],
                           source, operator)
        assert partial.returncode == 5, partial
        report = json.loads(partial.stdout)["details"]
        assert 0 < report["imported"] < len(tickets), report
        assert native(tk, source, operator, "show", tickets[-1])["status"] == "closed"
        assert shim.ok(sys.executable, [*command, "preimport"], source, operator)["imported"] == len(tickets) - report["imported"]

    archive = directory / "archive"
    arguments = [*command, "start", "--archive", str(archive), "--replay-corpus", str(corpus),
                 *(arg for machine in MACHINES for arg in ("--machine", machine))]
    if project == "by":
        arguments += ["--scotty-stopped"]
    if scenario == "success" and project == "by":
        # Only the operator drains on this first attempt. After the timeout,
        # reuse the staged import and retry with all three machines.
        timeout_arguments = [str(directory / "timeout-archive") if arg == str(archive) else arg
                             for arg in arguments]
        result = shim.run(sys.executable, [*timeout_arguments, "--pause-timeout", "8"], source, operator)
        assert result.returncode == 1, result
        assert "Timed out waiting for Beads drains" in result.stdout, result
        assert native(tk, source, operator, "state")["cutover"] == "before"
        print(f"{project}: a missing drain returned the project to Beads")

    writer = (["update", tickets[0], "--title", "Write in flight"] if scenario == "success"
              else ["delete", tickets[0], "--force"])
    with ExitStack() as processes:
        legacy_write = processes.enter_context(running(
            bd, ["--sandbox", *writer], source, {**operator, "CUTOVER_WRITER": str(directory)}))
        wait_for(lambda: (directory / "entered").exists(), legacy_write)
        cutover = processes.enter_context(running(sys.executable, arguments, source, operator))
        state_path = f"/{project}/api/state"
        wait_for(lambda: shim.request(address, state_path)["cutover"] == "paused", cutover)
        pause = shim.request(address, state_path)
        events = f"/{project}/api/events?kind=machine&since={pause['seq']}"
        assert not shim.request(address, events)
        pending = processes.enter_context(running(
            sys.executable, [SCRIPTS / "enact.py", "backend"], source, machines[1]))
        peer = processes.enter_context(running(tk, ["--json", "drain"], source, machines[2]))
        finish(peer)
        drains = shim.request(address, events)
        assert all(event["machine"] != MACHINES[0] for event in drains), drains
        assert legacy_write.poll() is None
        (directory / "release").touch()
        finish(legacy_write)
        out, _ = finish(cutover, expected=0 if scenario == "success" else 1)
        result = json.loads(out)
        backend, _ = finish(pending)
        if scenario == "mismatch":
            assert result["details"]["differences"], result
            assert shim.request(address, state_path)["cutover"] == "before"
            assert json.loads(backend)["backend"] == "beads"
            print(f"{project}: a mismatch in the final comparison returned the project to Beads")
            return

    assert result["state"]["cutover"] == "after"
    assert result["final_import"]["imported"] == 1
    assert result["comparison"]["differences"] == []
    assert json.loads(backend)["backend"] == "server"
    shown, = shim.ok(bd, ["show", tickets[0], "--json"], source, operator)
    assert shown["title"] == "Write in flight"

    # Mimic an old machine publishing after the authority switch. Also make a
    # native edit to a different ticket: unchanged legacy rows must be ignored.
    checked(legacy, ["--sandbox", "label", "add", tickets[1], "late"], source, env)
    checked(legacy, ["--sandbox", "label", "remove", tickets[1], "late"], source, env)
    checked("dolt", ["push", "origin", "main"], database, env)
    native(tk, source, operator, "comment", tickets[2], "Native edit")
    reconcile = ["reconcile", "--archive", str(archive)]
    too_soon = shim.run(sys.executable, [*command, *reconcile], source, operator)
    assert too_soon.returncode == 1 and "Reconcile after" in too_soon.stdout, too_soon
    virtual_hour = ["-c", "import cutover; cutover.RECONCILE_DELAY *= 0; "
                           "raise SystemExit(cutover.main())", *reconcile]
    assert shim.ok(sys.executable, virtual_hour, source, operator)["imported"] == 1
    assert shim.ok(sys.executable, virtual_hour, source, operator)["imported"] == 0
    assert native(tk, source, operator, "show", tickets[2], "--comments")["comments"][0]["text"] == "Native edit"
    found = native(tk, source, operator, "show", tickets[1], "--history")
    events = {event["event_type"] for change in found["history"] for event in change["change"]}
    assert {"label_added", "label_removed"} <= events

    # A subsequent legacy change overlaps a native edit. It must be rejected
    # and the isolated late source retained for manual reconciliation.
    checked(legacy, ["--sandbox", "update", tickets[2], "--title", "Conflict"], source, env)
    checked("dolt", ["push", "origin", "main"], database, env)
    conflict = shim.run(sys.executable, virtual_hour, source, operator)
    assert conflict.returncode == 3 and tickets[2].split("-", 1)[1] in conflict.stdout, conflict
    assert len(list(archive.glob("late-*"))) == 1
    print(f"{project}: staged, drained, compared, switched, and reconciled the isolated stores")


def main():
    tk = Path(sys.argv[1]).resolve()
    found = Path(shutil.which("bd")).absolute()
    paired = found.with_name(f"bd-real{found.suffix}")
    legacy = Path(os.environ.get("ENACT_REAL_BD") or (paired if paired.is_file() else found))
    with tempfile.TemporaryDirectory(prefix="enact-cutover-") as scratch:
        directory = Path(scratch)
        gitconfig = directory / "gitconfig"
        gitconfig.write_text("[user]\nname = Rehearsal\nemail = rehearsal@example.com\n")
        home = directory / "legacy-home"
        home.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("BEADS_", "ENACT_"))}
        env.update({"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
                    "HOME": str(home), "APPDATA": str(home), "USERPROFILE": str(home),
                    "DOLT_ROOT_PATH": str(home), "BEADS_NO_METRICS": "1",
                    "ENACT_TK": str(tk), "ENACT_NO_SELF_UPDATE": "1",
                    "ENACT_SHIM_HELPER": str(SCRIPTS / "enact.py"), "ENACT_SHIM_PYTHON": sys.executable,
                    "PYTHONPATH": str(SCRIPTS), "CUTOVER_LEGACY": str(legacy)})
        if shutil.which("rustup"):
            env["RUSTUP_HOME"] = subprocess.check_output(["rustup", "show", "home"], text=True).strip()
        wrapper = directory / f"legacy{tk.suffix}"
        source = directory / "legacy.rs"
        source.write_text(WRITER)
        subprocess.run(["rustc", str(source), "-o", str(wrapper)], check=True)
        bd = directory / f"bd{tk.suffix}"
        subprocess.run([sys.executable, SCRIPTS / "install_bd_shim.py", tk, wrapper, bd], check=True)
        env["PATH"] = str(directory) + os.pathsep + env["PATH"]
        corpus = prepare_replay(directory)
        for scenario in ("mismatch", "success"):
            workspace = directory / scenario
            workspace.mkdir()
            with shim.simulator(workspace) as address, ThreadPoolExecutor(max_workers=2) as executor:
                projects = [executor.submit(exercise, tk, legacy, bd, workspace / project, project,
                                            address, env, corpus, scenario) for project in ("by", "en")]
                for project in projects:
                    project.result()


if __name__ == "__main__":
    main()
