//! The `aurora-ext/1` wire types (documents/EXTENSIONS_PROTOCOL.md, R477):
//! newline-delimited JSON-RPC 2.0 to `python -m aurora.exthost`. Only the
//! contract lives here; the client that runs the host is Phase 7.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::types::ToolSpec;

pub const PROTOCOL: &str = "aurora-ext/1";

#[derive(Debug, Serialize)]
pub struct Request<'a, P: Serialize> {
    pub jsonrpc: &'static str,
    pub id: u64,
    pub method: &'a str,
    pub params: P,
}

impl<'a, P: Serialize> Request<'a, P> {
    pub fn new(id: u64, method: &'a str, params: P) -> Self {
        Self { jsonrpc: "2.0", id, method, params }
    }
}

#[derive(Debug, Serialize)]
pub struct InitializeParams<'a> {
    pub protocol: &'a str,
    pub paths: Vec<String>,
    pub config: &'a Value,
}

#[derive(Debug, Deserialize)]
pub struct InitializeResult {
    pub protocol: String,
    pub tools: Vec<ToolSpec>,
    #[serde(default)]
    pub warnings: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct CallParams<'a> {
    pub name: &'a str,
    pub arguments: &'a crate::types::Args,
}

#[derive(Debug, Deserialize)]
pub struct CallResult {
    pub output: String,
    #[serde(default)]
    pub is_error: bool,
}

#[derive(Debug, Deserialize)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
}

#[derive(Debug, Deserialize)]
pub struct Response<R> {
    pub id: Option<u64>,
    pub result: Option<R>,
    pub error: Option<RpcError>,
}

pub mod codes {
    pub const PARSE: i64 = -32700;
    pub const INVALID_REQUEST: i64 = -32600;
    pub const UNKNOWN_METHOD: i64 = -32601;
    pub const BAD_PARAMS: i64 = -32602;
    pub const NOT_INITIALIZED: i64 = -32002;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_shape_is_json_rpc_2() {
        let cfg = serde_json::json!({"runtime": {}});
        let req = Request::new(1, "initialize", InitializeParams { protocol: PROTOCOL, paths: vec!["x".into()], config: &cfg });
        let v = serde_json::to_value(&req).unwrap();
        assert_eq!(v["jsonrpc"], "2.0");
        assert_eq!(v["params"]["protocol"], "aurora-ext/1");
    }

    #[test]
    fn parses_a_tool_error_answer() {
        let r: Response<CallResult> = serde_json::from_str(
            r#"{"jsonrpc":"2.0","id":3,"result":{"output":"[tool error: ValueError: bad]","is_error":true}}"#,
        )
        .unwrap();
        assert!(r.result.unwrap().is_error);
    }
}
