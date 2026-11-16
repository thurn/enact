use serde_json::{Value, json};
use std::fmt::Display;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use std::{env, fs, thread};
use url::Url;

pub struct Failure {
    pub code: u8,
    pub message: String,
    pub body: Option<Value>,
    pub status: Option<u16>,
}

impl Failure {
    pub fn new(code: u8, message: impl Display) -> Self {
        Self {
            code,
            message: message.to_string(),
            body: None,
            status: None,
        }
    }
}

pub type Result<T> = std::result::Result<T, Failure>;

pub fn usage(error: impl Display) -> Failure {
    Failure::new(1, error)
}

pub struct Client {
    base: Url,
    agent: ureq::Agent,
    pub actor: String,
    pub project: String,
    pub retry_for: Duration,
}

pub struct Reply {
    pub status: u16,
    pub body: String,
}

impl Reply {
    pub fn json(self) -> Result<Value> {
        if self.status == 304 {
            return Ok(json!({"unchanged": true}));
        }
        serde_json::from_str(&self.body).map_err(usage)
    }
}

impl Client {
    pub fn load(project: Option<&str>, actor: String, retry_for: Duration) -> Result<Self> {
        let here = env::current_dir().map_err(usage)?;
        let (path, config) = read_config(&here)?;
        let tickets = config.get("tickets").and_then(toml::Value::as_table);
        let field = |name| {
            tickets
                .and_then(|table| table.get(name))
                .and_then(toml::Value::as_str)
                .ok_or_else(|| usage(format!("{} needs [tickets].{name}", path.display())))
        };
        let project = match project {
            Some(project) => project,
            None => field("project")?,
        };
        let project_char = |byte: u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
        if !project.bytes().next().is_some_and(project_char)
            || !project
                .bytes()
                .all(|byte| project_char(byte) || byte == b'-')
        {
            return Err(usage("invalid project key"));
        }
        let mut base = Url::parse(field("url")?).map_err(usage)?;
        if !matches!(base.scheme(), "https" | "http")
            || base.host().is_none()
            || !base.username().is_empty()
            || base.password().is_some()
            || base.query().is_some()
            || base.fragment().is_some()
        {
            return Err(usage("tickets.url must be an HTTP or HTTPS server URL"));
        }
        base.path_segments_mut()
            .expect("HTTP URL")
            .pop_if_empty()
            .extend([project, "api"]);
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .max_redirects(0)
            .build()
            .into();
        Ok(Self {
            base,
            agent,
            actor,
            project: project.to_owned(),
            retry_for,
        })
    }

    pub fn url(&self, segments: &[&str], query: &[(&str, String)]) -> Url {
        let mut url = self.base.clone();
        url.path_segments_mut()
            .expect("HTTP URL")
            .extend(segments.iter().copied());
        if !query.is_empty() {
            url.query_pairs_mut().extend_pairs(query.iter());
        }
        url
    }

    pub fn get(&self, url: &Url, etag: Option<u64>, budget: Duration) -> Result<Reply> {
        self.request(url, None, true, etag, budget)
    }

    pub fn post(&self, url: &Url, body: &Value, replay_safe: bool) -> Result<Reply> {
        self.request(url, Some(body), replay_safe, None, self.retry_for)
    }

    fn request(
        &self,
        url: &Url,
        body: Option<&Value>,
        replay_safe: bool,
        etag: Option<u64>,
        budget: Duration,
    ) -> Result<Reply> {
        let authorization = body
            .map(|_| token().map(|token| format!("Bearer {token}")))
            .transpose()?;
        let encoded = body.map(Value::to_string);
        let mut retry = Backoff::new(budget);
        loop {
            let timeout = if budget.is_zero() {
                Duration::from_secs(10)
            } else {
                retry
                    .remaining()
                    .clamp(Duration::from_millis(1), Duration::from_secs(10))
            };
            let response = if let Some(encoded) = &encoded {
                self.agent
                    .post(url.as_str())
                    .header(
                        "Authorization",
                        authorization.as_deref().expect("write token"),
                    )
                    .header("X-Actor", &self.actor)
                    .header("Content-Type", "application/json")
                    .config()
                    .timeout_global(Some(timeout))
                    .build()
                    .send(encoded.as_bytes())
            } else {
                let mut request = self.agent.get(url.as_str());
                if let Some(etag) = etag {
                    request = request.header("If-None-Match", format!("\"{etag}\""));
                }
                request
                    .config()
                    .timeout_global(Some(timeout))
                    .build()
                    .call()
            };
            let failure = match response {
                Ok(mut response) => {
                    let status = response.status().as_u16();
                    let body = if status == 304 {
                        Ok(String::new())
                    } else {
                        response
                            .body_mut()
                            .with_config()
                            .limit(64 * 1024 * 1024)
                            .read_to_string()
                    };
                    match body {
                        Ok(body) if status == 304 || (200..300).contains(&status) => {
                            return Ok(Reply { status, body });
                        }
                        Ok(body) => {
                            let failure = http_failure(status, &body);
                            if failure.code != 5 && !(failure.code == 4 && replay_safe) {
                                return Err(failure);
                            }
                            failure
                        }
                        Err(error) => transport_failure(error, replay_safe)?,
                    }
                }
                Err(error) => transport_failure(error, replay_safe)?,
            };
            retry.pause_or(failure)?;
        }
    }
}

fn transport_failure(error: ureq::Error, replay_safe: bool) -> Result<Failure> {
    let before_send = matches!(
        error,
        ureq::Error::HostNotFound
            | ureq::Error::ConnectionFailed
            | ureq::Error::Timeout(ureq::Timeout::Resolve | ureq::Timeout::Connect)
    ) || matches!(&error, ureq::Error::Io(error) if error.kind() == std::io::ErrorKind::ConnectionRefused);
    match error {
        ureq::Error::Timeout(_)
        | ureq::Error::Io(_)
        | ureq::Error::HostNotFound
        | ureq::Error::ConnectionFailed => {
            let failure = Failure::new(4, format!("ticket server unavailable: {error}"));
            if replay_safe || before_send {
                Ok(failure)
            } else {
                Err(failure)
            }
        }
        _ => Err(usage(error)),
    }
}

pub fn http_failure(status: u16, body: &str) -> Failure {
    let parsed: Option<Value> = serde_json::from_str(body).ok();
    let daily_limit = status == 429
        && matches!(
            parsed.as_ref().and_then(|body| body["limit"].as_str()),
            Some("requests" | "rows_written")
        );
    let code = match status {
        409 => 3,
        _ if daily_limit => 5,
        500..=599 => 4,
        _ => 1,
    };
    let detail = parsed.as_ref().and_then(|body| body["error"].as_str());
    let message = match detail {
        Some(detail) => format!("ticket server returned HTTP {status}: {detail}"),
        None => format!("ticket server returned HTTP {status}"),
    };
    Failure {
        code,
        message,
        body: parsed,
        status: Some(status),
    }
}

pub fn read_config(here: &Path) -> Result<(PathBuf, toml::Table)> {
    for directory in here.ancestors() {
        let path = directory.join(".enact.toml");
        let result = fs::read_to_string(&path);
        match result {
            Ok(contents) => {
                let config = contents
                    .parse()
                    .map_err(|error| usage(format!("{}: {error}", path.display())))?;
                return Ok((path, config));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                if directory.join(".git").exists() {
                    break;
                }
            }
            Err(error) => return Err(usage(format!("{}: {error}", path.display()))),
        }
    }
    Err(usage("no repository .enact.toml found"))
}

pub fn token_path() -> Result<PathBuf> {
    let base = if cfg!(windows) {
        env::var_os("APPDATA").map(PathBuf::from)
    } else {
        env::home_dir().map(|home| home.join(".config"))
    };
    base.map(|path| path.join("enact").join("token"))
        .ok_or_else(|| usage("cannot locate the Enact token directory"))
}

fn token() -> Result<String> {
    let path = token_path()?;
    let token =
        fs::read_to_string(&path).map_err(|error| usage(format!("{}: {error}", path.display())))?;
    let token = token.trim();
    if token.is_empty() || !token.bytes().all(|byte| byte.is_ascii_graphic()) {
        return Err(usage(format!("{}: invalid bearer token", path.display())));
    }
    Ok(token.to_owned())
}

pub struct Backoff {
    start: Instant,
    budget: Duration,
    delay: Duration,
}

impl Backoff {
    pub fn new(budget: Duration) -> Self {
        Self {
            start: Instant::now(),
            budget,
            delay: Duration::from_millis(200),
        }
    }

    pub fn remaining(&self) -> Duration {
        self.budget.saturating_sub(self.start.elapsed())
    }

    pub fn pause_or(&mut self, failure: Failure) -> Result<()> {
        let remaining = self.remaining();
        if self.delay >= remaining {
            // Preserve the retry window even if it ends before the next scheduled attempt.
            thread::sleep(remaining);
            return Err(failure);
        }
        thread::sleep(self.delay);
        self.delay = (self.delay * 2).min(Duration::from_secs(10));
        Ok(())
    }
}
