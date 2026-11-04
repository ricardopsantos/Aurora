"""R345: turn a backend failure into a cause and a short list of fixes.

Pure text logic — no I/O — so the startup panel, `/status` and a failed turn
all say the same thing. Nothing here names a particular machine, network or
vendor beyond the software Aurora itself supports (llama.cpp, Ollama,
OpenRouter / any OpenAI-compatible server)."""

from dataclasses import dataclass, field

KINDS = ("refused", "timeout", "dns", "tls", "auth", "nokey", "notfound",
         "ratelimit", "other")


@dataclass
class Diagnosis:
    kind: str
    cause: str
    fixes: list[str] = field(default_factory=list)


def classify(err) -> str:
    """The failure kind for an exception or message."""
    low = str(err).lower()
    if any(s in low for s in ("certificate", "ssl", "tls", "handshake failure")) \
            and "timed out" not in low:
        return "tls"
    if "refused" in low or "errno 111" in low:
        return "refused"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if any(s in low for s in ("name or service", "nodename", "getaddrinfo",
                              "errno -2", "errno -3", "temporary failure in name")):
        return "dns"
    if "401" in low or "403" in low or "unauthorized" in low or "forbidden" in low \
            or "invalid api key" in low or "incorrect api key" in low:
        return "auth"
    if "404" in low or "not found" in low:
        return "notfound"
    if "429" in low or "rate limit" in low or "rate-limit" in low:
        return "ratelimit"
    return "other"


def diagnose(kind: str, *, provider: str = "", url: str = "", model: str = "",
             key_env: str = "", detail: str = "", is_ollama: bool = False,
             local: bool = True) -> Diagnosis:
    """Cause + fixes for `kind`. `local` says the backend is something the user
    runs themselves (a llama.cpp/Ollama server) rather than a hosted API."""
    where = f" at {url}" if url else ""
    cfg = f"base_url under providers → {provider}" if provider else "base_url"
    pick = "/model — pick another configured model"
    add = "/model add <org/model> — use a hosted OpenRouter model instead"
    if kind == "refused":
        start = ("start Ollama (`ollama serve`)" if is_ollama else
                 "start your llama.cpp server (or whichever server `base_url` points at)")
        return Diagnosis(kind, f"nothing is listening{where}",
                         [start, f"check the host and port in config.yaml ({cfg})",
                          pick, add])
    if kind == "timeout":
        return Diagnosis(kind, f"the server{where} did not answer in time",
                         ["check that the host is up and reachable (network / VPN)",
                          "a model that is still loading can be slow — retry in a moment",
                          "raise runtime.timeout in config.yaml for slow hardware", pick])
    if kind == "dns":
        return Diagnosis(kind, f"the host name{where} does not resolve",
                         [f"check the spelling of the host in config.yaml ({cfg})",
                          "check your DNS / VPN, or use the server's IP address", pick])
    if kind == "tls":
        return Diagnosis(kind, f"the TLS certificate{where} was rejected",
                         ["use the host name the certificate was issued for, not an IP",
                          "for a plain LAN server use http:// instead of https://",
                          f"check the date and time on this machine", pick])
    if kind == "auth":
        env = key_env or "the provider's key variable"
        return Diagnosis(kind, "the server rejected the API key",
                         [f"store a valid key: `aurora key set {key_env or '<ENV_VAR>'}`",
                          f"check that {env} holds the key for THIS provider "
                          "and has not expired", pick])
    if kind == "nokey":
        env = key_env or "<ENV_VAR>"
        return Diagnosis(kind, f"no API key is stored for {provider or 'this provider'}",
                         [f"`aurora key set {env}` (or export {env} in your shell)",
                          pick + (" (a local server needs no key)" if local else "")])
    if kind == "notfound":
        fix = ([f"`ollama pull {model}` — and check the exact name with `ollama list`"]
               if is_ollama else
               ["check the model name against what the server has loaded"])
        return Diagnosis(kind, f"the server{where} does not know this model or route",
                         fix + [f"check that base_url ends in /v1 ({cfg})", pick])
    if kind == "ratelimit":
        return Diagnosis(kind, "the provider is rate-limiting requests",
                         ["wait a moment and retry",
                          "free-tier models share a pool — add your own key or pick another", pick])
    return Diagnosis(kind, detail or "the backend could not be reached",
                     [f"check the server and config.yaml ({cfg})",
                      "/status shows the backend's current state", pick, add])


def render(d: Diagnosis, indent: str = "           ") -> list[str]:
    """Plain lines for the panel: `cause:` then bullet fixes."""
    out = [f"{indent}cause: {d.cause}"]
    for i, fix in enumerate(d.fixes):
        out.append(f"{indent}{'fix:   ' if i == 0 else '       '}• {fix}")
    return out
