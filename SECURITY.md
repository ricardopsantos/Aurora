# Security

Aurora runs tools on your machine: it edits files and runs shell commands when a model asks and you approve (or when auto-approve is on). Treat every way of reaching it as a way of running code as your user.

## The web page (`aurora --web`)
- **It is remote code execution if exposed.** Whoever reaches the port and holds `AURORA_WEB_TOKEN` can make the model run commands.
- The server refuses to start without `AURORA_WEB_TOKEN`. Use a long random value and keep it out of version control.
- The default bind is `127.0.0.1`. Any other bind prints a warning at start. Reach it through a TLS reverse proxy on a private network (a VPN, an SSO proxy), never directly from the internet.
- Auto-approve on the page is remembered across restarts. With it off, every write and command asks first; reads never ask.
- The web denylist (`~/.aurora/web-denylist.txt`, one regex per line; built-in defaults block `rm -rf`, `sudo`, `git push`, `mkfs`, `dd if=`, shutdown/reboot, `curl … | sh`) refuses matching calls without asking, even with auto-approve on.
- Plan mode runs nothing that changes state.
- `/health` needs no token and reveals only booleans and the version.

## The terminal
- Approvals, the allowlist and the denylist live in `~/.aurora/`. "Always deny" wins over "always allow".
- Tool output that looks like a secret is shown to you and redacted before it reaches the model, unless you choose otherwise.

## Reporting a vulnerability
Please open a private security advisory on the repository (or contact the maintainer privately) rather than a public issue. Include the version (`aurora --version`), how to reproduce it, and the impact.
