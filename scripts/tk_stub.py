#!/usr/bin/env python3
"""Local SQLite stand-in for Enact's ticket client during the server port.

Set TK_STUB_DB to a file in the test's temporary directory. Invoke this with
Python or as an executable in place of tk. All replies are JSON.
"""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys

SCHEMA = Path(__file__).resolve().parents[1] / "server" / "contract" / "schema.sql"
ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
BLOCKING = {"blocks", "conditional-blocks", "waits-for"}
EDGE_KINDS = BLOCKING | {"related", "discovered-from"}
EVENT_KINDS = {"session", "ci", "machine", "cutover", "usage"}
TICKET_STATUSES = {"open", "in_progress", "blocked", "deferred", "closed"}
TICKET_TYPES = {"task", "epic", "bug", "feature", "chore", "decision", "spike", "story", "milestone"}
TICKET_INPUT = {
    "title", "body", "acceptance", "design", "notes", "type", "status", "priority",
    "parent", "assignee", "last", "labels", "metadata", "defer_until",
}
JSON_FIELDS = {"labels", "metadata", "change", "payload", "waiters", "data"}
LIMITS = {"requests": 100_000, "rows_written": 100_000, "rows_read": 5_000_000}


class Conflict(Exception):
    def __init__(self, reply):
        self.reply = reply


def decoded(row):
    result = dict(row)
    for field in JSON_FIELDS.intersection(result):
        result[field] = json.loads(result[field])
    return result


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, db, actor, project, machine):
        self.db = db
        self.actor = actor
        self.project = project
        self.machine = machine
        self.now = datetime.now(timezone.utc)
        self.at = self.now.isoformat()

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def seq(self):
        return int(self.meta("seq", 0))

    def advance(self):
        seq = self.seq() + 1
        self.set_meta("seq", seq)
        return seq

    def resolve(self, name):
        name = name.removeprefix(self.project + "-")
        row = self.db.execute("SELECT id FROM aliases WHERE alias = ?", (name,)).fetchone()
        return row["id"] if row else name

    def lookup(self, name):
        row = self.db.execute("SELECT * FROM tickets WHERE id = ?", (name,)).fetchone()
        return decoded(row) if row else None

    def ticket(self, name):
        ticket = self.lookup(self.resolve(name))
        if ticket is None:
            raise ValueError(f"Unknown ticket {name}")
        return ticket

    def ancestors(self, ticket):
        while ticket["parent"]:
            ticket = self.ticket(ticket["parent"])
            yield ticket

    def descendants(self, name):
        rows = self.db.execute(
            """WITH RECURSIVE descendants(id) AS (
                SELECT id FROM tickets WHERE parent = ?
                UNION ALL
                SELECT tickets.id FROM tickets JOIN descendants
                    ON tickets.parent = descendants.id
            )
            SELECT tickets.* FROM tickets JOIN descendants USING (id) ORDER BY id""",
            (name,),
        )
        return [decoded(row) for row in rows]

    def edges(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM edges ORDER BY src, dst")]

    def children(self, name):
        return [decoded(row) for row in self.db.execute(
            "SELECT * FROM tickets WHERE parent = ? ORDER BY id", (name,)
        )]

    def readiness(self, ticket):
        name = ticket["id"]
        blockers = []

        def block(target, rule):
            blockers.append({"id": target, "rule": rule})

        if ticket["status"] != "open" or (
            ticket["defer_until"] and timestamp(ticket["defer_until"]) >= self.now
        ):
            block(name, 1)
        sources = [(name, 2)] + [(parent["id"], 3) for parent in self.ancestors(ticket)]
        for source, rule in sources:
            for edge in self.db.execute("SELECT * FROM edges WHERE src = ? ORDER BY dst", (source,)):
                if edge["kind"] not in BLOCKING:
                    continue
                target = self.ticket(edge["dst"])
                if target["status"] != "closed":
                    block(target["id"], rule)
                elif any(child["status"] != "closed" for child in self.descendants(target["id"])):
                    block(target["id"], rule)
        if ticket["last"] and ticket["parent"]:
            for sibling in self.descendants(ticket["parent"]):
                if not sibling["last"] and sibling["status"] != "closed":
                    block(sibling["id"], 4)
        for child in self.children(name):
            if child["status"] != "closed":
                block(child["id"], 5)
        return {"ready": not blockers, "blocked_by": blockers}

    def show(self, args):
        tickets = [self.show_ticket(id, args.comments, args.history) for id in args.ids]
        return tickets[0] if len(tickets) == 1 else tickets

    def comments(self, args):
        return self.comments_for(self.ticket(args.id)["id"])

    def comments_for(self, id):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM comments WHERE ticket = ? ORDER BY n", (id,)
        )]

    def show_ticket(self, id, comments, history):
        ticket = self.ticket(id)
        name = ticket["id"]
        ticket["edges"] = [edge for edge in self.edges() if name in (edge["src"], edge["dst"])]
        ticket["children"] = [child["id"] for child in self.children(name)]
        ticket.update(self.readiness(ticket))
        if comments:
            ticket["comments"] = self.comments_for(name)
        if history:
            ticket["history"] = [decoded(row) for row in self.db.execute(
                "SELECT * FROM history WHERE ticket = ? ORDER BY seq", (name,)
            )]
        return ticket

    def list(self, args):
        filters = {key: getattr(args, key) for key in ("status", "type", "parent", "assignee")}
        if filters["parent"] is not None:
            filters["parent"] = self.resolve(filters["parent"])
        ancestor = self.resolve(args.ancestor) if args.ancestor else None
        candidates = self.descendants(ancestor) if ancestor else map(
            decoded, self.db.execute("SELECT * FROM tickets ORDER BY id")
        )
        results = []
        for ticket in candidates:
            if ticket["status"] == "closed" and not (args.all or args.status == "closed"):
                continue
            if any(value is not None and ticket[key] != value for key, value in filters.items()):
                continue
            if not set(args.label).issubset(ticket["labels"]):
                continue
            readiness = self.readiness(ticket)
            if not args.ready or readiness["ready"]:
                results.append({**ticket, **readiness})
        return results[:args.limit or None]

    def graph(self, args):
        if args.if_none_match == self.seq():
            return {"unchanged": True}
        tickets = self.db.execute("SELECT * FROM tickets WHERE ? OR status != 'closed' ORDER BY id",
                                  (args.all,))
        omit = {"body", "design", "notes"}
        rows = {}
        for row in tickets:
            ticket = decoded(row)
            readiness = self.readiness(ticket)
            rows[ticket["id"]] = {
                **{k: v for k, v in ticket.items() if k not in omit},
                "ready": readiness["ready"],
            }
            if ticket["status"] != "closed":
                rows[ticket["id"]]["blocked_by"] = [item for item in readiness["blocked_by"] if item["rule"] != 1]
        edges = [edge for edge in self.edges() if edge["src"] in rows and edge["dst"] in rows]
        return {"tickets": list(rows.values()), "edges": edges, "seq": self.seq()}

    def changes(self, name, fields, old):
        seq = self.advance()
        if old is None:
            fields = {**fields, "id": name, "created_at": self.at, "created_by": self.actor}
        fields = {**fields, "updated_at": self.at, "version": seq}
        values = [json.dumps(value) if field in JSON_FIELDS else value
                  for field, value in fields.items()]
        if old is None:
            columns = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            self.db.execute(f"INSERT INTO tickets ({columns}) VALUES ({marks})", values)
        else:
            assignments = ", ".join(f"{field} = ?" for field in fields)
            self.db.execute(f"UPDATE tickets SET {assignments} WHERE id = ?", [*values, name])
        change = {key: [old.get(key) if old else None, value] for key, value in fields.items()}
        self.db.execute("INSERT INTO history VALUES (?, ?, ?, ?, ?)",
                        (name, seq, self.actor, self.at, json.dumps(change)))

    def input_fields(self, fields):
        unknown = fields.keys() - TICKET_INPUT
        if unknown:
            raise ValueError(f"Unknown ticket field: {sorted(unknown)[0]}")
        if "status" in fields and fields["status"] not in TICKET_STATUSES:
            raise ValueError("Unknown ticket status")
        if "type" in fields and fields["type"] not in TICKET_TYPES:
            raise ValueError("Unknown ticket type")
        if "priority" in fields and fields["priority"] not in range(5):
            raise ValueError("Priority must be between 0 and 4")
        if fields.get("defer_until"):
            timestamp(fields["defer_until"])
        return fields

    def create(self, op, ref):
        key = op.get("request_key")
        key = f"create:{key}" if key is not None else None
        saved = json.loads(self.meta(key, "null")) if key else None
        if saved and timestamp(saved["at"]) + timedelta(days=1) > self.now:
            return saved["id"]
        fields = self.input_fields(dict(op["fields"]))
        parent_id = op.get("parent", fields.get("parent"))
        parent = self.ticket(ref(parent_id)) if parent_id else None
        fields["parent"] = parent["id"] if parent else None
        if parent:
            name = f'{parent["id"]}.{parent["next_child"]}'
            self.changes(parent["id"], {"next_child": parent["next_child"] + 1}, parent)
        else:
            while True:
                name = secrets.choice(ALPHABET[10:]) + "".join(secrets.choice(ALPHABET) for _ in range(3))
                alias = self.db.execute("SELECT 1 FROM aliases WHERE alias = ?", (name,)).fetchone()
                if alias is None and self.lookup(name) is None:
                    break
        self.changes(name, fields, None)
        for dep in op.get("deps", []):
            edge = {"on": dep} if isinstance(dep, str) else dep
            self.dependency(name, edge, ref, remove=False)
        if key:
            self.set_meta(key, json.dumps({"id": name, "at": self.at}))
        return name

    def dependency(self, name, op, ref, *, remove):
        target = self.ticket(ref(op["on"]))["id"]
        kind = op.get("kind", "blocks")
        if kind not in EDGE_KINDS:
            raise ValueError("Unknown dependency type")
        if remove:
            self.db.execute("DELETE FROM edges WHERE src = ? AND dst = ? AND (? IS NULL OR kind = ?)",
                            (name, target, op.get("kind"), kind))
        else:
            self.db.execute("INSERT OR REPLACE INTO edges VALUES (?, ?, ?)", (name, target, kind))
        ticket = self.ticket(name)
        self.changes(name, {}, ticket)

    def update(self, ticket, op, ref):
        fields = self.input_fields(dict(op.get("set", {})))
        if fields.get("parent"):
            parent = self.ticket(ref(fields["parent"]))
            if any(candidate["id"] == ticket["id"]
                   for candidate in [parent, *self.ancestors(parent)]):
                raise ValueError("A ticket cannot become its own ancestor")
            fields["parent"] = parent["id"]
        if "add_labels" in op or "remove_labels" in op:
            labels = dict.fromkeys(fields.get("labels", ticket["labels"]) + op.get("add_labels", []))
            fields["labels"] = [label for label in labels if label not in op.get("remove_labels", [])]
        if "set_meta" in op or "unset_meta" in op:
            metadata = {**fields.get("metadata", ticket["metadata"]), **op.get("set_meta", {})}
            fields["metadata"] = {k: v for k, v in metadata.items() if k not in op.get("unset_meta", [])}
        self.changes(ticket["id"], fields, ticket)

    def close(self, ticket, reason):
        name = ticket["id"]
        self.changes(name, {"status": "closed", "closed_at": self.at, "close_reason": reason}, ticket)
        closed = []
        for ancestor in self.ancestors(ticket):
            parent = ancestor["id"]
            if any(child["status"] != "closed" for child in self.descendants(parent)):
                break
            if ancestor["status"] != "closed":
                self.changes(parent, {"status": "closed", "closed_at": self.at,
                                      "close_reason": reason}, ancestor)
                closed.append(parent)
        return closed

    def apply(self, op, ref, index):
        action = op["op"]
        if action == "create":
            return {"id": self.create(op, ref)}
        ticket = self.ticket(ref(op["id"]))
        name = ticket["id"]
        result = {"id": name}
        if action == "event":
            self.write_event(op["kind"], {**op["data"], "ticket": name})
        elif action == "update":
            self.update(ticket, op, ref)
        elif action == "claim":
            if ticket["assignee"] not in (None, self.actor):
                raise Conflict({"failed": index, "current": ticket})
            self.changes(name, {"assignee": self.actor, "status": "in_progress"}, ticket)
        elif action == "close":
            result["closed"] = self.close(ticket, op["reason"])
            if op.get("include_ready"):
                candidates = self.db.execute("SELECT * FROM tickets WHERE status = 'open' ORDER BY id")
                result["ready"] = [row["id"] for row in candidates
                                   if self.readiness(decoded(row))["ready"]]
        elif action == "reopen":
            self.changes(name, {"status": "open", "closed_at": None, "close_reason": None}, ticket)
        elif action == "defer":
            timestamp(op["until"])
            self.changes(name, {"status": "open", "defer_until": op["until"]}, ticket)
        elif action == "comment":
            self.db.execute(
                """INSERT INTO comments (ticket, n, author, at, text)
                    SELECT ?, COALESCE(MAX(n), 0) + 1, ?, ?, ?
                    FROM comments WHERE ticket = ?""",
                (name, self.actor, self.at, op["text"], name),
            )
            self.changes(name, {}, ticket)
        elif action in ("dep_add", "dep_remove"):
            self.dependency(name, op, ref, remove=action == "dep_remove")
        else:
            raise ValueError(f"Unknown batch operation {action}")
        return result

    def batch(self, batch):
        for index, condition in enumerate(batch.get("if", [])):
            if condition.keys() == {"seq"}:
                current = None
                passed = self.seq() == condition["seq"]
            else:
                fields = condition.keys() - {"id"}
                if len(fields) != 1 or not fields <= {"assignee", "status", "version"}:
                    raise ValueError("Unknown batch precondition")
                field = next(iter(fields))
                current = self.lookup(self.resolve(condition["id"]))
                passed = current is not None and current[field] == condition[field]
            if not passed:
                raise Conflict({"failed": index, "current": current})
        created = {}

        def ref(name):
            return created[name] if name.startswith("$") else name

        results = []
        for index, op in enumerate(batch["ops"]):
            result = self.apply(op, ref, index)
            results.append(result)
            if op["op"] == "create":
                created[f"${index}"] = result["id"]
        return {"seq": self.seq(), "results": results}

    def leases(self):
        return [decoded(row) for row in self.db.execute(
            "SELECT * FROM leases WHERE holder != '' ORDER BY name"
        )]

    def lease(self, args):
        row = self.db.execute("SELECT * FROM leases WHERE name = ?", (args.name,)).fetchone()
        lease = decoded(row) if row else None
        active = lease and lease["holder"] and timestamp(lease["expires_at"]) > self.now
        holder = args.holder or self.actor
        waiters = [waiter for waiter in (lease["waiters"] if lease else [])
                   if self.now - timestamp(waiter["since"]) < timedelta(seconds=60)]
        first = waiters[0]["holder"] if waiters else None
        if args.op == "acquire":
            permitted = lease["holder"] == holder if active else first in (None, holder)
            if not permitted:
                if args.no_queue:
                    raise Conflict({"holder": lease["holder"], "expires_at": lease["expires_at"],
                                    "seq": self.seq()})
                names = [waiter["holder"] for waiter in waiters]
                if holder in names:
                    waiters[names.index(holder)]["since"] = self.at
                else:
                    names.append(holder)
                    waiters.append({"holder": holder, "since": self.at})
                self.db.execute("UPDATE leases SET waiters = ? WHERE name = ?",
                                (json.dumps(waiters), args.name))
                self.advance()
                # A denied acquire still records the caller's place in line.
                self.db.commit()
                raise Conflict({"holder": lease["holder"], "expires_at": lease["expires_at"],
                                "position": names.index(holder) + 1, "seq": self.seq()})
        elif (not active or lease["holder"] != holder
              or (args.if_revision is not None and lease["payload"].get("revision") != args.if_revision)):
            raise Conflict({"holder": lease["holder"] if lease else None,
                            "expires_at": lease["expires_at"] if lease else None})
        if args.op == "release":
            if waiters:
                self.db.execute("UPDATE leases SET holder = '', expires_at = ?, waiters = ? WHERE name = ?",
                                (self.at, json.dumps(waiters), args.name))
            else:
                self.db.execute("DELETE FROM leases WHERE name = ?", (args.name,))
            self.advance()
            return lease
        if args.ttl <= 0:
            raise ValueError("Lease TTL must be positive")
        payload = json.loads(args.payload) if args.payload else (lease["payload"] if active else {})
        reply = {
            "name": args.name, "holder": holder, "machine": self.machine,
            "expires_at": (self.now + timedelta(seconds=args.ttl)).isoformat(),
            "payload": payload, "waiters": [waiter for waiter in waiters if waiter["holder"] != holder],
        }
        self.db.execute("INSERT OR REPLACE INTO leases VALUES (?, ?, ?, ?, ?, ?)",
                        (args.name, holder, self.machine, reply["expires_at"],
                         json.dumps(payload), json.dumps(reply["waiters"])))
        self.advance()
        return reply

    def write_event(self, kind, data):
        if kind not in EVENT_KINDS:
            raise ValueError(f"Unknown event kind {kind}")
        seq = self.advance()
        self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                        (seq, self.at, kind, self.machine, self.actor, json.dumps(data)))
        return {"seq": seq, "at": self.at, "kind": kind, "machine": self.machine,
                "actor": self.actor, "data": data}

    def event(self, args):
        return self.write_event(args.kind, json.loads(args.data))

    def events(self, args):
        order = "ASC" if args.since is not None else "DESC"
        return [decoded(row) for row in self.db.execute(
            f"""SELECT * FROM events WHERE seq > ? AND (? IS NULL OR kind = ?)
                ORDER BY seq {order} LIMIT ?""",
            (args.since or 0, args.kind, args.kind, args.limit),
        )]

    def state(self, args):
        state = self.meta("cutover", "before")
        desired = args.set_cutover
        if desired and desired != state:
            if (state, desired) not in {("before", "paused"), ("paused", "before"), ("paused", "after")}:
                raise ValueError(f"Cannot move cutover from {state} to {desired}")
            state = desired
            self.set_meta("cutover", state)
            self.set_meta("cutover_at", self.at)
            self.advance()
        return {"cutover": state, "cutover_at": self.meta("cutover_at"), "seq": self.seq(),
                "day": self.now.date().isoformat(), "requests": 0, "rows_written": 0,
                "limits": LIMITS}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", default="test")
    parser.add_argument("--project", default="by")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--retry-for", type=float, default=120)
    commands = parser.add_subparsers(dest="command", required=True)

    show = commands.add_parser("show")
    show.add_argument("ids", nargs="+")
    show.add_argument("--comments", action="store_true")
    show.add_argument("--history", action="store_true")

    comments = commands.add_parser("comments")
    comments.add_argument("id")

    for name in ("list", "ready"):
        listing = commands.add_parser(name)
        for field in ("status", "type", "parent", "ancestor", "assignee"):
            listing.add_argument("--" + field)
        listing.add_argument("--label", action="append", default=[])
        listing.add_argument("--all", action="store_true")
        listing.add_argument("--ready", action="store_true", default=name == "ready")
        listing.add_argument("-n", "--limit", type=int)

    graph = commands.add_parser("graph")
    graph.add_argument("--all", action="store_true")
    graph.add_argument("--if-none-match", type=int)
    commands.add_parser("batch")
    lease = commands.add_parser("lease")
    lease.add_argument("op", choices=["acquire", "renew", "release"])
    lease.add_argument("name")
    lease.add_argument("--holder")
    lease.add_argument("--ttl", type=int, default=180)
    lease.add_argument("--payload")
    lease.add_argument("--if-revision")
    lease.add_argument("--no-queue", action="store_true")
    commands.add_parser("leases")

    event = commands.add_parser("event")
    event.add_argument("kind", choices=sorted(EVENT_KINDS))
    event.add_argument("data")
    events = commands.add_parser("events")
    events.add_argument("--kind", choices=sorted(EVENT_KINDS))
    events.add_argument("--since", type=int)
    events.add_argument("--limit", type=int, default=-1)
    state = commands.add_parser("state")
    state.add_argument("--set-cutover", choices=["before", "paused", "after"])
    wait = commands.add_parser("wait")
    wait.add_argument("mode", choices=["change"])
    wait.add_argument("--since", required=True, type=int)
    wait.add_argument("--timeout", required=True, type=int)
    return parser.parse_args()


def main():
    try:
        args = parse_args()
    except SystemExit as error:
        return 1 if error.code else 0
    if trace := os.environ.get("TK_STUB_TRACE"):
        with Path(trace).open("a", encoding="utf-8") as handle:
            handle.write(args.command + "\n")
    db_path = os.environ.get("TK_STUB_DB")
    if not db_path:
        print("Set TK_STUB_DB to a temporary SQLite file", file=sys.stderr)
        return 1
    try:
        with closing(sqlite3.connect(Path(db_path))) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone():
                for statement in SCHEMA.read_text(encoding="utf-8").split(";"):
                    db.execute(statement)
            db.commit()
            with db:
                db.execute("BEGIN IMMEDIATE")
                store = Store(db, args.actor, args.project, os.environ.get("TK_STUB_MACHINE", "local"))
                if args.command == "batch":
                    result = store.batch(json.load(sys.stdin))
                elif args.command == "wait":
                    if args.since == store.seq():
                        return 6  # No transport in the SQLite stand-in.
                    result = {"resync": True}
                elif args.command == "ready":
                    result = store.list(args)
                elif args.command == "leases":
                    result = getattr(store, args.command)()
                else:
                    result = getattr(store, args.command)(args)
    except Conflict as conflict:
        print(json.dumps(conflict.reply))
        return 3
    except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
