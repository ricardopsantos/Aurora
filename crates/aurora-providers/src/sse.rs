//! Server-sent-event line splitting over an arbitrary chunking of the body.

#[derive(Default)]
pub struct LineBuffer {
    buf: Vec<u8>,
}

fn decode(mut line: &[u8]) -> String {
    if line.last() == Some(&b'\r') {
        line = &line[..line.len() - 1];
    }
    String::from_utf8_lossy(line).into_owned()
}

impl LineBuffer {
    /// Feed bytes; returns every complete line (without `\n` / `\r\n`).
    pub fn push(&mut self, bytes: &[u8]) -> Vec<String> {
        // R557: only the new bytes are searched, and the consumed lines leave the buffer in one drain (a body of
        // many short lines in one chunk used to be rescanned and shifted once per line)
        let old = self.buf.len();
        self.buf.extend_from_slice(bytes);
        let mut out = Vec::new();
        let mut start = 0;
        let mut from = old;
        while let Some(off) = self.buf[from..].iter().position(|&b| b == b'\n') {
            let i = from + off;
            out.push(decode(&self.buf[start..i]));
            start = i + 1;
            from = start;
        }
        if start > 0 {
            self.buf.drain(..start);
        }
        out
    }

    /// Whatever is left once the body ends without a final newline.
    pub fn finish(&mut self) -> Option<String> {
        if self.buf.is_empty() {
            return None;
        }
        let rest = std::mem::take(&mut self.buf);
        Some(String::from_utf8_lossy(&rest).into_owned())
    }
}

/// The payload of a `data:` line, or None for any other SSE field.
pub fn data_of(line: &str) -> Option<&str> {
    line.strip_prefix("data:").map(str::trim)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_across_chunks_and_keeps_utf8_whole() {
        let mut b = LineBuffer::default();
        let text = "data: {\"a\":\"olá\"}\r\n\ndata: [DONE]\n".as_bytes();
        let (one, two) = text.split_at(14);
        let mut lines = b.push(one);
        lines.extend(b.push(two));
        assert_eq!(lines, ["data: {\"a\":\"olá\"}", "", "data: [DONE]"]);
        assert_eq!(data_of(&lines[2]), Some("[DONE]"));
        assert!(b.finish().is_none());
    }

    #[test]
    fn many_lines_in_one_chunk_and_a_carried_partial() {
        let mut b = LineBuffer::default();
        let body: String = (0..1000).map(|i| format!("data: {i}\n")).collect();
        let lines = b.push(format!("{body}data: par").as_bytes());
        assert_eq!(lines.len(), 1000);
        assert_eq!(lines[999], "data: 999");
        assert!(b.push(b"tial").is_empty());
        assert_eq!(b.push(b"\r\n"), ["data: partial"]);
        assert_eq!(b.push(b"tail"), Vec::<String>::new());
        assert_eq!(b.finish().as_deref(), Some("tail"));
    }
}
