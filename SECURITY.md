# Security

Aurora runs tools on your machine: it edits files and runs shell commands when a model asks and you approve (or when auto-approve is on). Treat every way of reaching it as a way of running code as your user.

## The web page (`aurora --web`)
- **It is remote code execution if exposed.** Whoever reaches the port and holds `AURORA_WEB_TOKEN` can make the model run commands.
- The server refuses to start without `AURORA_WEB_TOKEN`. Use a long random value and keep it out of version control.
- The default bind is `127.0.0.1`. Any other bind prints a warning at start. Reach it through a TLS reverse proxy on a private network (a VPN, an SSO proxy), never directly from the internet.
- Auto-approve is off until you turn it on: every write and command asks first; reads never ask. The switch is remembered across restarts.
- **Commands run in a sandbox.** Each command the model runs from the page sees the machine read-only, except the workspace, Aurora's `workspace/` folder (uploads and outputs), `~/.cache` and a private `/tmp`. It cannot see `~/.ssh`, `~/.gnupg`, cloud and Docker credentials, `/etc/environment` or Aurora's own keys and config. It cannot reach the Docker socket, the user's D-Bus or `systemd --user`, the ssh/gpg agents, tmux, or other processes, and cannot change `.git/hooks` or `.git/config`. Linux uses bubblewrap (`apt install bubblewrap`); macOS uses `sandbox-exec`. The sandbox is tried once at start; if it does not work on the machine, the server says so and every command is asked on the page, even with auto-approve on. Settings live under `runtime:` in config.yaml (`sandbox: web|all|off`, `sandbox_network`, `sandbox_writable`, `sandbox_hide`; see `aurora/sandbox.py`). Commands you type yourself in bash mode are never sandboxed.
- The web denylist (`~/.aurora/web-denylist.txt`, one regex per line; built-in defaults block `rm -rf`, `sudo`, `git push`, `mkfs`, `dd if=`, shutdown/reboot, `curl … | sh`, Docker, ssh, secret files) refuses matching calls without asking, even with auto-approve on. It follows `cd` and a command's `cwd`, but it is a pattern list: the sandbox is the boundary, the denylist an early "no".
- Plan mode runs nothing that changes state.
- `/health` needs no token and reveals only booleans and the version.
- Share links (`/shared/<token>`) need no bearer token: the link is the key. A link shows the chat as it was when it was shared (prompts and final answers, never tool output or thinking), expires after 7 days, and can be revoked. Sharing the same chat again updates the copy to now.
- After 10 wrong tokens within 10 minutes a client is refused (HTTP 429) for 5 minutes, without its token being checked. Behind a local reverse proxy the client is the last `X-Forwarded-For` address.
- The pages send a Content-Security-Policy that lets them run only their own script and talk only to the server they came from.

## The terminal
- Approvals, the allowlist and the denylist live in `~/.aurora/`. "Always deny" wins over "always allow".
- The terminal's commands run outside the sandbox unless config.yaml says `runtime: sandbox: all` (the default sandboxes the web page only, since the terminal asks before each command and often needs your ssh keys for `git push`).
- Tool output that looks like a secret is shown to you and redacted before it reaches the model, unless you choose otherwise.

## Reporting a vulnerability
Please open a private security advisory on the repository (or contact the maintainer privately) rather than a public issue. Include the version (`aurora --version`), how to reproduce it, and the impact.
