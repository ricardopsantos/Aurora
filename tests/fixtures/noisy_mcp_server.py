#!/usr/bin/env python3
"""An MCP server that dies during the handshake after explaining why on
stderr (R153).

This is what a real misconfigured server looks like: the process starts
fine (so there is no spawn `OSError` to carry the message), then fails on a
missing dependency and exits. Before R153 the client reported only "closed
the connection" and threw the explanation away.

With `--flood`, it instead writes far more than a pipe buffer holds (500
lines x ~200 chars, ~100KB) and then speaks the protocol normally: a client
that captures stderr without draining it would wedge the child on a blocked
`write` and never complete the handshake."""
import runpy
import sys
from pathlib import Path


def main():
    if "--flood" in sys.argv:
        for i in range(500):
            sys.stderr.write(f"log line {i:04d} " + "x" * 200 + "\n")
        sys.stderr.flush()
        # then behave like the ordinary fake server
        sys.argv = [sys.argv[0]]
        runpy.run_path(str(Path(__file__).parent / "fake_mcp_server.py"),
                       run_name="__main__")
        return
    sys.stderr.write("Error: Cannot find module '@modelcontextprotocol/sdk'\n")
    sys.stderr.write("    at Module._resolveFilename (node:internal)\n")
    sys.stderr.flush()
    sys.exit(1)


if __name__ == "__main__":
    main()
