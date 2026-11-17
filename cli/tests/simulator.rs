use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Output, Stdio};
use tempfile::TempDir;

struct Simulator {
    child: Child,
    directory: TempDir,
}

impl Drop for Simulator {
    fn drop(&mut self) {
        drop(self.child.stdin.take());
        let _ = self.child.wait();
    }
}

impl Simulator {
    fn start() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path();
        std::fs::create_dir_all(path.join("repo/subdirectory")).unwrap();
        let token_dir = if cfg!(windows) {
            "appdata/enact"
        } else {
            "home/.config/enact"
        };
        std::fs::create_dir_all(path.join(token_dir)).unwrap();
        std::fs::write(path.join(token_dir).join("token"), "simulator-token\n").unwrap();
        let script = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/simulator.mjs");
        let mut child = Command::new("node")
            .arg(script)
            .current_dir(path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("start Workers simulator (run npm ci in server/ first)");
        let mut address = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut address)
            .unwrap();
        assert!(
            address.starts_with("http://"),
            "Workers simulator did not start"
        );
        std::fs::write(
            path.join("repo/.enact.toml"),
            format!("[tickets]\nproject = 'by'\nurl = '{}'\n", address.trim(),),
        )
        .unwrap();
        std::fs::write(path.join("repo/body.txt"), "body on disk\n").unwrap();
        Self { child, directory }
    }

    fn command(&self, args: &[&str], input: Option<Value>) -> Output {
        self.output(&[&["--json"], args].concat(), input)
    }

    fn output(&self, args: &[&str], input: Option<Value>) -> Output {
        let mut process = Command::new(env!("CARGO_BIN_EXE_tk"));
        let base = self.directory.path();
        process
            .current_dir(base.join("repo/subdirectory"))
            .env("HOME", base.join("home"))
            .env("APPDATA", base.join("appdata"))
            .env_remove("ENACT_TICKETS_URL")
            .args(["--retry-for", "0", "--actor", "test actor"])
            .args(args);
        if let Some(input) = input {
            let mut child = process
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap();
            write!(child.stdin.take().unwrap(), "{input}").unwrap();
            child.wait_with_output().unwrap()
        } else {
            process.output().unwrap()
        }
    }

    fn ok(&self, args: &[&str]) -> Value {
        let output = self.command(args, None);
        decode(args, &output, 0)
    }
}

fn decode(args: &[&str], output: &Output, code: i32) -> Value {
    assert_eq!(
        output.status.code(),
        Some(code),
        "{args:?}: {}{}",
        String::from_utf8_lossy(&output.stderr),
        String::from_utf8_lossy(&output.stdout)
    );
    if output.stdout.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&output.stdout).unwrap()
    }
}

#[test]
fn commands_use_the_worker_protocol() {
    let sim = Simulator::start();
    for (args, expected) in [
        (&["show", "Tk7mq"][..], "Tk7mq  open  Simulator ticket"),
        (&["create", "--title", "Readable output"], "Tk7mq"),
        (
            &[
                "--project",
                "display",
                "wait",
                "ticket",
                "Tk7mq",
                "--since",
                "2",
                "--timeout",
                "5",
            ],
            "Tk7mq",
        ),
    ] {
        let output = sim.output(args, None);
        assert!(output.status.success(), "{args:?}: {:?}", output.stderr);
        assert_eq!(
            String::from_utf8(output.stdout).unwrap().trim_end(),
            expected
        );
    }
    assert_eq!(
        sim.ok(&["show", "by-k7mq", "--comments", "--history"])["trace"],
        json!({"project":"by","route":"tickets","target":"by-k7mq",
               "args":[["readable","1"],["comments","1"],["history","1"]],"actor":null,"auth":null,"etag":null,"body":null})
    );
    let list = sim.ok(&[
        "--project",
        "en",
        "list",
        "--status",
        "closed",
        "--type",
        "task",
        "--parent",
        "m7mq",
        "--ancestor",
        "q7mq",
        "--label",
        "one & two",
        "--label",
        "three",
        "--assignee",
        "test actor",
        "--all",
        "--limit",
        "5",
    ]);
    assert_eq!(list[0]["trace"]["project"], "en");
    assert_eq!(
        list[0]["trace"]["args"],
        json!([
            ["ancestor", "q7mq"],
            ["label", "one & two"],
            ["label", "three"],
            ["readable", "1"],
            ["status", "closed"],
            ["type", "task"],
            ["parent", "m7mq"],
            ["assignee", "test actor"],
            ["limit", "5"],
            ["all", "1"]
        ])
    );
    assert_eq!(
        sim.ok(&["ready", "--label", "one"])[0]["trace"]["args"],
        json!([["label", "one"], ["ready", "1"], ["readable", "1"]])
    );
    assert_eq!(
        sim.ok(&["ready", "--limit", "10"])[0]["trace"]["args"],
        json!([["limit", "10"], ["ready", "1"], ["readable", "1"]])
    );
    assert_eq!(
        sim.ok(&["ready", "--limit", "0"])[0]["trace"]["args"],
        json!([["ready", "1"], ["readable", "1"]])
    );
    let graph = sim.ok(&["graph"]);
    assert!(graph["edges"].is_array());
    assert_eq!(
        sim.ok(&["graph", "--if-none-match", graph["etag"].as_str().unwrap()]),
        json!({"unchanged": true})
    );

    let create = sim.ok(&[
        "create",
        "--title",
        "child",
        "--parent",
        "aaaa",
        "--type",
        "task",
        "-p",
        "1",
        "--labels",
        "a,b",
        "--body-file",
        "../body.txt",
        "--design-file",
        "../body.txt",
        "--acceptance",
        "passes",
        "--deps",
        "bbbb",
        "--last",
    ]);
    let trace = &create["trace"];
    assert_eq!(trace["actor"], "test actor");
    assert_eq!(trace["auth"], "Bearer simulator-token");
    let create = &trace["body"]["ops"][0];
    assert_eq!(create["parent"], "aaaa");
    assert_eq!(create["deps"], json!(["bbbb"]));
    assert_eq!(create["request_key"].as_str().unwrap().len(), 32);
    assert_eq!(
        create["fields"],
        json!({"title":"child","type":"task","priority":1,"labels":["a","b"],
        "body":"body on disk\n","design":"body on disk\n","acceptance":"passes","last":1})
    );

    let output = sim.output(
        &[
            "--project",
            "details",
            "show",
            "Tk7mq",
            "--comments",
            "--history",
        ],
        None,
    );
    assert!(output.status.success());
    let text = String::from_utf8(output.stdout).unwrap();
    for required in [
        "Description:\nFirst line\nSecond line",
        "Reproduced and fixed",
        "Use the existing helper",
        "Keep imported aliases",
        "Taaaa",
        "Tbbbb",
        "Tk7mq.1",
        "Handoff: next steps",
        "History:",
    ] {
        assert!(text.contains(required), "{required:?} missing from {text}");
    }
    let summary = sim.ok(&[
        "--project",
        "details",
        "show",
        "Tk7mq",
        "--comments",
        "--history",
        "--brief",
    ]);
    assert_eq!(summary["id"], "Tk7mq");
    assert_eq!(summary["title"], "Full ticket");
    assert_eq!(summary["version"], 7);
    assert_eq!(summary["metadata"]["notes"], "Keep user metadata");
    assert_eq!(summary["blocked_by"][0]["id"], "Tbbbb");
    assert!(summary.get("body").is_none());
    assert!(summary.get("comments").is_none());
    assert!(summary.get("history").is_none());
    assert_eq!(summary["edges"][0]["dst"], "Tbbbb");

    let output = sim.command(&["update", "Tk7mq", "--body-file", "../absent.txt"], None);
    assert!(
        decode(&[], &output, 1)["error"]
            .as_str()
            .unwrap()
            .contains("absent.txt")
    );
    let changed = sim.ok(&[
        "update",
        "Tk7mq",
        "--description",
        "Revised body",
        "--design-file",
        "../body.txt",
    ]);
    assert_eq!(
        changed["trace"]["body"]["ops"][0]["set"],
        json!({"body":"Revised body", "design":"body on disk\n"})
    );
    let output = sim.command(
        &["comments", "add", "Tk7mq", "--body-file", "-"],
        Some(json!("comment from stdin")),
    );
    let changed = decode(&[], &output, 0);
    assert_eq!(
        changed["trace"]["body"]["ops"][0]["text"],
        "\"comment from stdin\""
    );

    let update = sim.ok(&[
        "update",
        "k7mq",
        "--title",
        "updated",
        "--status",
        "in_progress",
        "-p",
        "2",
        "--assignee",
        "",
        "--parent",
        "",
        "--add-label",
        "new",
        "--remove-label",
        "old",
        "--notes",
        "some notes",
        "--set-metadata",
        "a=b=c",
        "--unset-metadata",
        "unused",
        "--no-last",
        "--if-version",
        "3",
        "--if-assignee",
        "",
    ]);
    let body = &update["trace"]["body"];
    assert_eq!(
        body["if"],
        json!([{"id":"k7mq","version":3},{"id":"k7mq","assignee":null}])
    );
    assert_eq!(
        body["ops"][0],
        json!({"op":"update","id":"k7mq",
        "set":{"title":"updated","status":"in_progress","priority":2,"assignee":null,
               "parent":null,"notes":"some notes","last":0},
        "add_labels":["new"],"remove_labels":["old"],"set_meta":{"a":"b=c"},"unset_meta":["unused"]})
    );

    for (args, op) in [
        (vec!["claim", "k7mq"], json!({"op":"claim","id":"k7mq"})),
        (
            vec!["close", "k7mq", "--reason", "done"],
            json!({"op":"close","id":"k7mq","reason":"done"}),
        ),
        (vec!["reopen", "k7mq"], json!({"op":"reopen","id":"k7mq"})),
        (
            vec!["defer", "k7mq", "--until", "2026-11-17"],
            json!({"op":"defer","id":"k7mq","until":"2026-11-17"}),
        ),
        (
            vec!["comments", "add", "k7mq", "-f", "../body.txt"],
            json!({"op":"comment","id":"k7mq","text":"body on disk\n"}),
        ),
        (
            vec!["comments", "add", "k7mq", "a comment"],
            json!({"op":"comment","id":"k7mq","text":"a comment"}),
        ),
        (
            vec!["dep", "add", "k7mq", "bbbb", "-t", "related"],
            json!({"op":"dep_add","id":"k7mq","on":"bbbb","kind":"related"}),
        ),
        (
            vec!["dep", "remove", "k7mq", "bbbb"],
            json!({"op":"dep_remove","id":"k7mq","on":"bbbb"}),
        ),
    ] {
        assert_eq!(sim.ok(&args)["trace"]["body"], json!({"if":[],"ops":[op]}));
    }

    let args = ["batch"];
    let body = json!({"if":[{"seq":12}],"ops":[{"op":"claim","id":"k7mq"},{"op":"update","id":"k7mq","set_meta":{"session":"worker"}}]});
    let output = sim.command(&args, Some(body.clone()));
    assert_eq!(decode(&args, &output, 0)["trace"]["body"], body);
    assert_eq!(
        sim.ok(&[
            "lease",
            "acquire",
            "vizier/epic/one",
            "--ttl",
            "60",
            "--payload",
            "{\"x\":1}"
        ])["body"],
        json!({"op":"acquire","holder":"test actor","ttl_s":60,"payload":{"x":1}})
    );
    assert_eq!(
        sim.ok(&["lease", "renew", "staging", "--holder", "custom"])["body"],
        json!({"op":"renew","holder":"custom","ttl_s":180})
    );
    assert_eq!(
        sim.ok(&["lease", "release", "staging"])["body"],
        json!({"op":"release","holder":"test actor"})
    );
    assert_eq!(sim.ok(&["leases"])["route"], "leases");
    assert_eq!(
        sim.ok(&["event", "ci", "{\"result\":\"pass\"}"])["body"],
        json!({"kind":"ci","data":{"result":"pass"}})
    );
    assert_eq!(
        sim.ok(&["events", "--kind", "ci", "--since", "2", "--limit", "3"])["args"],
        json!([["kind", "ci"], ["since", "2"], ["limit", "3"]])
    );
    assert_eq!(
        sim.ok(&["state", "--set-cutover", "paused"])["cutover"],
        "paused"
    );
    assert_eq!(sim.ok(&["state"])["cutover"], "before");

    assert_eq!(
        sim.ok(&[
            "--project",
            "wait",
            "wait",
            "ready",
            "--label",
            "test",
            "--timeout",
            "30"
        ])[0]["id"],
        "Tk7mq"
    );
    assert_eq!(
        sim.ok(&[
            "wait",
            "ticket",
            "by-k7mq",
            "--status",
            "closed",
            "--timeout",
            "30"
        ])["id"],
        "Tk7mq"
    );
    assert_eq!(
        sim.ok(&["wait", "ticket", "k7mq", "--since", "2", "--timeout", "30"])["tickets"][0],
        "Tk7mq"
    );
    assert_eq!(
        sim.ok(&[
            "wait",
            "lease",
            "vizier/epic/one",
            "--since",
            "2",
            "--timeout",
            "30"
        ])["leases"][0],
        "vizier/epic/one"
    );
    assert_eq!(
        sim.ok(&["wait", "change", "--since", "2", "--timeout", "30"])["seq"],
        3
    );
    let args = ["wait", "change", "--since", "99", "--timeout", "0.5"];
    decode(&args, &sim.command(&args, None), 6);

    let output = sim.command(&["export"], None);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "{\"id\":\"Taaaa\"}\n{\"ticket\":{\"id\":\"Tbbbb\"}}\n"
    );
    assert_eq!(sim.ok(&["bench", "--samples", "2"])["samples"], 2);
    assert_eq!(sim.ok(&["version"])["version"], env!("CARGO_PKG_VERSION"));

    let args = ["claim", "conflict"];
    assert_eq!(decode(&args, &sim.command(&args, None), 3)["failed"], 0);
    let args = ["show", "missing"];
    decode(&args, &sim.command(&args, None), 1);
    let args = ["--project", "quota", "claim", "k7mq"];
    assert_eq!(
        decode(&args, &sim.command(&args, None), 5)["limit"],
        "rows_written"
    );
    let args = [
        "--project",
        "unsafe",
        "comments",
        "add",
        "k7mq",
        "never replay this",
        "--retry-for",
        "5",
    ];
    decode(&args, &sim.command(&args, None), 4);
    sim.ok(&[
        "--project",
        "retry",
        "create",
        "--title",
        "deduplicated",
        "--retry-for",
        "5",
    ]);
    let trace = sim.ok(&["--project", "trace", "state"]);
    let trace = trace.as_array().unwrap();
    let attempts = |project| {
        trace
            .iter()
            .filter(|entry| entry["project"] == project)
            .collect::<Vec<_>>()
    };
    assert_eq!(attempts("unsafe").len(), 1);
    let attempts = attempts("retry");
    assert_eq!(attempts.len(), 2);
    assert_eq!(attempts[0]["body"], attempts[1]["body"]);
}

#[test]
#[ignore = "runs in slow CI with real Git clones and the SQLite service"]
fn enact_coordinates_through_sqlite() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let result = Command::new("python3")
        .args(["-m", "unittest", "-q", "tests.test_ticket_coordination"])
        .env("ENACT_TEST_TK", env!("CARGO_BIN_EXE_tk"))
        .current_dir(root.parent().unwrap())
        .output()
        .expect("run the Enact and SQLite integration scenario");
    assert!(
        result.status.success(),
        "{}{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
}
