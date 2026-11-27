//! JSON exactly as Python's `json.dumps` writes it: `", "` and `": "`
//! separators, and with `ensure_ascii` every non-ASCII character as a
//! lowercase `\uXXXX` escape (surrogate pairs above the BMP). Python readers
//! depend on the spacing (`Session.iter_records` prefilters on the literal
//! `"event": "<name>"`), and both Aurora's send the model byte-identical
//! tool-call arguments.

use serde::Serialize;
use serde_json::ser::Formatter;
use std::io;

struct PyFormatter {
    ascii: bool,
}

impl Formatter for PyFormatter {
    fn begin_array_value<W: ?Sized + io::Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if first { Ok(()) } else { w.write_all(b", ") }
    }
    fn begin_object_key<W: ?Sized + io::Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if first { Ok(()) } else { w.write_all(b", ") }
    }
    fn begin_object_value<W: ?Sized + io::Write>(&mut self, w: &mut W) -> io::Result<()> {
        w.write_all(b": ")
    }
    fn write_string_fragment<W: ?Sized + io::Write>(&mut self, w: &mut W, fragment: &str) -> io::Result<()> {
        if !self.ascii || fragment.is_ascii() {
            return w.write_all(fragment.as_bytes());
        }
        let mut units = [0u16; 2];
        for c in fragment.chars() {
            if c.is_ascii() {
                w.write_all(&[c as u8])?;
            } else {
                for u in c.encode_utf16(&mut units) {
                    write!(w, "\\u{u:04x}")?;
                }
            }
        }
        Ok(())
    }
}

fn dump<T: Serialize>(value: &T, ascii: bool) -> String {
    let mut out = Vec::new();
    let mut ser = serde_json::Serializer::with_formatter(&mut out, PyFormatter { ascii });
    value.serialize(&mut ser).expect("JSON values always serialise");
    String::from_utf8(out).expect("serde_json writes UTF-8")
}

/// `json.dumps(v, ensure_ascii=False, separators=(", ", ": "))`: session records.
pub fn dumps<T: Serialize>(value: &T) -> String {
    dump(value, false)
}

/// `json.dumps(v)`: Python's defaults, as used for tool-call arguments.
pub fn dumps_ascii<T: Serialize>(value: &T) -> String {
    dump(value, true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_python_spacing_and_keeps_unicode() {
        let v = json!({"ts": "t", "event": "user", "text": "olá\n\"x\"", "n": [1, 2], "e": {}});
        assert_eq!(dumps(&v), r#"{"ts": "t", "event": "user", "text": "olá\n\"x\"", "n": [1, 2], "e": {}}"#);
    }

    #[test]
    fn ascii_mode_escapes_like_python() {
        // json.dumps({"p": "olá 😀\u0001"}) == '{"p": "ol\\u00e1 \\ud83d\\ude00\\u0001"}'
        let want = concat!(r#"{"p": "ol"#, "\\", "u00e1 ", "\\", "ud83d", "\\", "ude00", "\\", r#"u0001"}"#);
        assert_eq!(dumps_ascii(&json!({"p": "olá 😀\u{1}"})), want);
    }
}
