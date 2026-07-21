//! HTTP client for the taiko-trainer server — `POST /api/v1/replays` and
//! `GET /api/v1/whoami`. Uses reqwest with rustls-tls so we don't have to
//! ship OpenSSL on Windows.

use crate::config::Config;
use reqwest::multipart::{Form, Part};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::path::Path;

/// The outcome of one upload attempt. Mirrors the Python `UploadOutcome`
/// shape so the retry-vs-skip decisions stay identical.
#[derive(Clone, Debug, Serialize)]
pub struct UploadOutcome {
    pub ok: bool,
    pub skip_reason: Option<String>,    // "duplicate" | "foreign_replay" | "unauth"
    pub replay_id: Option<i64>,
    pub summary: Option<serde_json::Value>,
    pub retryable: bool,
    pub error: Option<String>,
}

impl UploadOutcome {
    fn err(msg: impl Into<String>, retryable: bool) -> Self {
        Self {
            ok: false,
            skip_reason: None,
            replay_id: None,
            summary: None,
            retryable,
            error: Some(msg.into()),
        }
    }
    fn skip(reason: &str) -> Self {
        Self {
            ok: false,
            skip_reason: Some(reason.into()),
            replay_id: None,
            summary: None,
            retryable: false,
            error: None,
        }
    }
}

pub async fn upload_one(client: &Client, cfg: &Config, path: &Path) -> UploadOutcome {
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("unknown.osr")
        .to_string();

    let bytes = match tokio::fs::read(path).await {
        Ok(b) => b,
        Err(e) => return UploadOutcome::err(format!("read: {}", e), false),
    };

    let part = Part::bytes(bytes)
        .file_name(file_name)
        .mime_str("application/x-osu-replay")
        .unwrap();
    let form = Form::new().part("file", part);

    let url = format!("{}/api/v1/replays", cfg.server_url);
    let resp = client
        .post(url)
        .header("Authorization", format!("Bearer {}", cfg.api_token))
        .multipart(form)
        .timeout(std::time::Duration::from_secs(60))
        .send()
        .await;

    let resp = match resp {
        Ok(r) => r,
        Err(e) => return UploadOutcome::err(format!("network: {}", e), true),
    };

    let status = resp.status().as_u16();
    let body_text = resp.text().await.unwrap_or_default();

    match status {
        200 | 201 => {
            let json: serde_json::Value = serde_json::from_str(&body_text).unwrap_or(serde_json::json!({}));
            let replay_id = json.get("replay_id").and_then(|v| v.as_i64());
            UploadOutcome {
                ok: true,
                skip_reason: None,
                replay_id,
                summary: Some(json),
                retryable: false,
                error: None,
            }
        }
        401 => {
            let mut o = UploadOutcome::skip("unauth");
            o.error = Some(body_text.chars().take(200).collect());
            o
        }
        403 => {
            let mut o = UploadOutcome::skip("foreign_replay");
            o.error = Some(body_text.chars().take(200).collect());
            o
        }
        409 => UploadOutcome::skip("duplicate"),
        500..=599 => UploadOutcome::err(
            format!("HTTP {}: {}", status, body_text.chars().take(200).collect::<String>()),
            true,
        ),
        _ => UploadOutcome::err(
            format!("HTTP {}: {}", status, body_text.chars().take(200).collect::<String>()),
            false,
        ),
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Whoami {
    pub username: String,
    pub user_id: i64,
    pub avatar_url: Option<String>,
    #[serde(default)]
    pub cover_url: Option<String>,
    pub country_code: Option<String>,
    #[serde(default)]
    pub global_rank: Option<i64>,
    pub style: Option<String>,
    pub server_url: Option<String>,
}

/// One replay the server has stored for the authenticated user. Used by
/// the Replays screen to cross-reference "is this local .osr on the
/// server?" via content_hash.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MyReplay {
    pub id: i64,
    #[serde(default)]
    pub content_hash: Option<String>,
    #[serde(default)]
    pub map_md5: Option<String>,
    #[serde(default)]
    pub played_at: Option<String>,
    #[serde(default)]
    pub map_title: Option<String>,
    #[serde(default)]
    pub map_version: Option<String>,
    #[serde(default)]
    pub mods: Option<String>,
    #[serde(default)]
    pub accuracy: Option<f64>,
}

#[derive(Clone, Debug, Deserialize)]
struct MyReplaysResp {
    #[serde(default)]
    username: Option<String>,
    replays: Vec<MyReplay>,
}

/// The Home leaderboard-band payload — the user's current skill snapshot
/// plus their rank on the total-skill leaderboard.
///
/// `has_data` is false for fresh users who haven't uploaded any rateable
/// replays yet; frontend renders an empty-state prompt in that case.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MySkill {
    pub has_data: bool,
    #[serde(default)]
    pub rank: Option<i64>,
    #[serde(default)]
    pub replays: Option<i64>,
    #[serde(default)]
    pub speed: Option<f64>,
    #[serde(default)]
    pub stamina: Option<f64>,
    #[serde(default)]
    pub gimmick: Option<f64>,
    #[serde(default)]
    pub technical: Option<f64>,
    #[serde(default)]
    pub consistency: Option<f64>,
    #[serde(default)]
    pub reading: Option<f64>,
    #[serde(default)]
    pub total: Option<f64>,
}

/// GET /api/v1/whoami with the bearer token. Returns None if the server
/// doesn't recognize the token (401), the endpoint isn't there (404), or
/// the network fails. Callers treat None as "identity unknown, show
/// anonymous state" and let the user re-enter the token from Settings.
pub async fn whoami(client: &Client, cfg: &Config) -> Option<Whoami> {
    let resp = client
        .get(format!("{}/api/v1/whoami", cfg.server_url))
        .header("Authorization", format!("Bearer {}", cfg.api_token))
        .timeout(std::time::Duration::from_secs(15))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let mut who: Whoami = resp.json().await.ok()?;
    if who.server_url.is_none() {
        who.server_url = Some(cfg.server_url.clone());
    }
    Some(who)
}

/// GET /api/v1/me/replays — the authenticated user's stored replays.
/// Returns (username, list). username lets the frontend build /replay/
/// links without a separate whoami round-trip.
pub async fn my_replays(client: &Client, cfg: &Config) -> Option<(String, Vec<MyReplay>)> {
    let resp = client
        .get(format!("{}/api/v1/me/replays", cfg.server_url))
        .header("Authorization", format!("Bearer {}", cfg.api_token))
        .timeout(std::time::Duration::from_secs(30))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let body: MyReplaysResp = resp.json().await.ok()?;
    Some((body.username.unwrap_or_default(), body.replays))
}

/// GET /api/v1/me/skill — the authenticated user's skill snapshot + rank.
/// Returns None on network error or non-2xx (frontend treats that as
/// "not available right now", keeps last-known value in the store).
pub async fn my_skill(client: &Client, cfg: &Config) -> Option<MySkill> {
    let resp = client
        .get(format!("{}/api/v1/me/skill", cfg.server_url))
        .header("Authorization", format!("Bearer {}", cfg.api_token))
        .timeout(std::time::Duration::from_secs(20))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    resp.json::<MySkill>().await.ok()
}

/// One reqwest client we reuse for the whole session — connection pooling,
/// HTTP/2 keep-alive, etc.
pub fn build_client() -> Client {
    Client::builder()
        .user_agent(format!("taiko-uploader/{} (tauri)", env!("CARGO_PKG_VERSION")))
        .build()
        .expect("reqwest client build")
}
