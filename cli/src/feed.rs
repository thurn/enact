use crate::args::{Wait, WaitMode};
use crate::client::{Backoff, Client, Failure, Result, http_failure, usage};
use serde_json::{Value, json};
use std::io;
use std::net::{TcpStream, ToSocketAddrs};
use std::time::{Duration, Instant};
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Error, HandshakeError, Message, WebSocket, client_tls};
use url::Url;

type Socket = WebSocket<MaybeTlsStream<TcpStream>>;

pub fn wait(client: &Client, args: Wait) -> Result<Value> {
    let deadline = Instant::now()
        .checked_add(Duration::from_secs_f64(args.timeout.unwrap_or(120.0)))
        .ok_or_else(|| usage("wait timeout is too large"))?;
    let mut target = Target::new(client, args.mode);
    let anchored = args.since.is_some() || target.probes();
    let mut seq = args.since.unwrap_or_default();
    if !anchored {
        let state = client
            .get(
                &client.url(&["state"], &[]),
                None,
                within(deadline, client.retry_for)?,
            )?
            .json()?;
        seq = state["seq"]
            .as_u64()
            .ok_or_else(|| usage("state has no sequence"))?;
    }
    if let Some(result) = target.probe(client, deadline)? {
        return Ok(result);
    }
    let mut url = client.url(&["feed"], &[]);
    let scheme = if url.scheme() == "https" { "wss" } else { "ws" };
    url.set_scheme(scheme)
        .expect("HTTP and WebSocket URLs share the same syntax");
    let mut retry = Backoff::new(within(deadline, client.retry_for)?);
    loop {
        let connect_for = if client.retry_for.is_zero() {
            Duration::from_secs(10)
        } else {
            retry
                .remaining()
                .clamp(Duration::from_millis(1), Duration::from_secs(10))
        };
        let connect_for = connect_for.min(remaining(deadline)?);
        let failure = match subscribe(&url, seq, connect_for) {
            Ok(mut socket) => {
                retry = Backoff::new(within(deadline, client.retry_for)?);
                match receive(client, &mut socket, &mut target, &mut seq, deadline) {
                    Ok(result) => {
                        let _ = socket.close(None);
                        return Ok(result);
                    }
                    Err(failure) if failure.code == 4 => failure,
                    Err(failure) => return Err(failure),
                }
            }
            Err(failure) if matches!(failure.code, 4 | 5) => failure,
            Err(failure) => return Err(failure),
        };
        retry.pause_or(failure).map_err(|failure| {
            if Instant::now() >= deadline {
                timed_out()
            } else {
                failure
            }
        })?;
    }
}

enum Target {
    Ready(Vec<(&'static str, String)>),
    Ticket {
        id: String,
        path: Url,
        status: Option<String>,
    },
    Lease(String),
    Change,
}

impl Target {
    fn new(client: &Client, mode: WaitMode) -> Self {
        match mode {
            WaitMode::Ready(filters) => {
                let mut query = crate::commands::ready_query(filters);
                query.push(("ready", "1".into()));
                Self::Ready(query)
            }
            WaitMode::Ticket { id, status } => Self::Ticket {
                path: client.url(&["tickets", &id], &[]),
                id: id
                    .strip_prefix('T')
                    .or_else(|| id.strip_prefix(&format!("{}-", client.project)))
                    .unwrap_or(&id)
                    .to_owned(),
                status,
            },
            WaitMode::Lease { name } => Self::Lease(name),
            WaitMode::Change => Self::Change,
        }
    }

    fn probes(&self) -> bool {
        matches!(
            self,
            Self::Ready(_)
                | Self::Ticket {
                    status: Some(_),
                    ..
                }
        )
    }

    fn probe(&mut self, client: &Client, deadline: Instant) -> Result<Option<Value>> {
        match self {
            Self::Ready(query) => {
                let reply = client
                    .get(
                        &client.url(&["tickets"], query),
                        None,
                        within(deadline, client.retry_for)?,
                    )?
                    .json()?;
                let tickets = reply
                    .as_array()
                    .ok_or_else(|| usage("ready response is not a list"))?;
                Ok((!tickets.is_empty()).then_some(reply))
            }
            Self::Ticket {
                path,
                id,
                status: Some(status),
            } => {
                let reply = match client.get(path, None, within(deadline, client.retry_for)?) {
                    Err(error) if error.status == Some(404) => return Ok(None),
                    reply => reply?.json()?,
                };
                if let Some(canonical) = reply["id"].as_str() {
                    *id = canonical.to_owned();
                }
                Ok((reply["status"] == status.as_str()).then_some(reply))
            }
            _ => Ok(None),
        }
    }

    fn matches(&self, message: &Value) -> bool {
        if message["resync"] == true {
            return true;
        }
        let contains = |key: &str, name: &str| {
            message[key]
                .as_array()
                .is_some_and(|items| items.iter().any(|item| item == name))
        };
        match self {
            Self::Ready(_) => message["tickets"]
                .as_array()
                .is_some_and(|items| !items.is_empty()),
            Self::Ticket { id, .. } => contains("tickets", id),
            Self::Lease(name) => contains("leases", name),
            Self::Change => true,
        }
    }
}

fn receive(
    client: &Client,
    socket: &mut Socket,
    target: &mut Target,
    seq: &mut u64,
    deadline: Instant,
) -> Result<Value> {
    loop {
        set_timeout(tcp_stream(socket.get_mut()), remaining(deadline)?).map_err(offline)?;
        let message = match socket.read() {
            Ok(Message::Text(text)) => serde_json::from_str::<Value>(&text).map_err(usage)?,
            Ok(Message::Close(_)) => return Err(offline("WebSocket closed")),
            Ok(Message::Ping(_)) => {
                socket.flush().map_err(socket_failure)?;
                continue;
            }
            Ok(_) => continue,
            Err(error) => {
                if Instant::now() >= deadline {
                    return Err(timed_out());
                }
                return Err(socket_failure(error));
            }
        };
        let resync = message["resync"] == true;
        let received = message["seq"].as_u64();
        if !resync {
            match received {
                Some(new_seq) if new_seq <= *seq => continue,
                Some(_) => {}
                None => return Err(usage("feed message has no sequence")),
            }
        }
        if target.matches(&message) {
            if !target.probes() {
                return Ok(message);
            }
            if let Some(result) = target.probe(client, deadline)? {
                return Ok(result);
            }
        }
        // Advance only after a successful probe so a reconnect can replay
        // changes we could not check. A resync may omit the sequence.
        if let Some(new_seq) = received {
            *seq = (*seq).max(new_seq);
        }
    }
}

fn subscribe(url: &Url, seq: u64, timeout: Duration) -> Result<Socket> {
    let host = match url.host() {
        Some(url::Host::Ipv6(address)) => address.to_string(),
        Some(host) => host.to_string(),
        None => return Err(usage("feed URL has no host")),
    };
    let port = url
        .port()
        .unwrap_or(if url.scheme() == "wss" { 443 } else { 80 });
    let start = Instant::now();
    let mut last = offline("no addresses resolved for the feed");
    for address in (host.as_str(), port).to_socket_addrs().map_err(offline)? {
        let remaining = timeout.saturating_sub(start.elapsed());
        if remaining.is_zero() {
            break;
        }
        match TcpStream::connect_timeout(&address, remaining) {
            Ok(stream) => {
                let remaining = timeout
                    .saturating_sub(start.elapsed())
                    .max(Duration::from_millis(1));
                set_timeout(&stream, remaining).map_err(offline)?;
                let (mut socket, _) =
                    client_tls(url.as_str(), stream).map_err(|error| match error {
                        HandshakeError::Failure(error) => socket_failure(error),
                        HandshakeError::Interrupted(_) => {
                            offline("WebSocket handshake interrupted")
                        }
                    })?;
                socket
                    .send(Message::Text(json!({"since": seq}).to_string().into()))
                    .map_err(socket_failure)?;
                return Ok(socket);
            }
            Err(error) => last = offline(error),
        }
    }
    Err(last)
}

fn tcp_stream(stream: &mut MaybeTlsStream<TcpStream>) -> &TcpStream {
    match stream {
        MaybeTlsStream::Plain(stream) => stream,
        MaybeTlsStream::Rustls(stream) => &stream.sock,
        _ => unreachable!("only plain and rustls sockets are enabled"),
    }
}

fn set_timeout(stream: &TcpStream, timeout: Duration) -> io::Result<()> {
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))
}

fn socket_failure(error: Error) -> Failure {
    match error {
        Error::Http(response) => {
            let body = response
                .body()
                .as_deref()
                .and_then(|body| std::str::from_utf8(body).ok())
                .unwrap_or("");
            http_failure(response.status().as_u16(), body)
        }
        Error::Io(_)
        | Error::ConnectionClosed
        | Error::AlreadyClosed
        | Error::Protocol(tungstenite::error::ProtocolError::ResetWithoutClosingHandshake) => {
            offline(error)
        }
        _ => usage(error),
    }
}

fn offline(error: impl std::fmt::Display) -> Failure {
    Failure::new(4, format!("ticket change feed unavailable: {error}"))
}

fn timed_out() -> Failure {
    Failure::new(6, "wait timed out")
}

fn remaining(deadline: Instant) -> Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|time| !time.is_zero())
        .ok_or_else(timed_out)
}

fn within(deadline: Instant, budget: Duration) -> Result<Duration> {
    Ok(remaining(deadline)?.min(budget))
}
