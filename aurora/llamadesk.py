"""LlamaDesk client (R3) — an optional local model-library loading agent.
Aurora never manages llama-server itself; switching a library model goes
through LlamaDesk's API and is GLOBAL (evicts the model for every other
consumer of that server), so the UI must confirm eviction before calling
switch()."""

import time

import httpx


class LlamaDeskError(Exception):
    pass


class LlamaDesk:
    # 5s: off-LAN the picker must degrade to config models quickly, not hang
    def __init__(self, base_url: str, token: str = "", timeout: float = 5):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _get(self, path: str) -> dict:
        try:
            r = httpx.get(f"{self.base_url}{path}", headers=self._headers(),
                          timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise LlamaDeskError(f"LlamaDesk {path}: {e}") from e

    def models(self) -> list[str]:
        """Library model names available to load."""
        data = self._get("/api/models")
        items = data.get("models", data) if isinstance(data, dict) else data
        out = []
        for m in items or []:
            out.append(m.get("name", str(m)) if isinstance(m, dict) else str(m))
        return out

    def models_detail(self) -> list[dict]:
        """[{'name', 'ctx_native'}] — native ctx per gguf. Falls back to the
        plain name list (ctx_native None) on older LlamaDesk servers."""
        try:
            data = self._get("/api/models/detail")
            return data.get("models", [])
        except LlamaDeskError:
            return [{"name": n, "ctx_native": None} for n in self.models()]

    def status(self) -> dict:
        """{'loaded': <name>|None, 'busy': bool, ...} — shape tolerant."""
        data = self._get("/api/status")
        return data if isinstance(data, dict) else {}

    def loaded_model(self) -> str | None:
        s = self.status()
        return s.get("loaded") or s.get("model") or s.get("current") or None

    def busy(self) -> bool:
        """A switch/load is in flight. /api/status has no such flag —
        the real signal is /api/switch/progress's `running`."""
        try:
            return bool(self._get("/api/switch/progress").get("running"))
        except LlamaDeskError:
            return False

    def switch(self, name: str, ctx: int = 65536, ngl: str = "auto") -> None:
        """Request a model switch. Caller must have confirmed eviction and
        checked for in-flight work (status) first. ctx matters: LlamaDesk's
        API defaults to 8192, far too small for agent work (and any other
        consumer syncing its own settings from LlamaDesk would propagate
        that too-small value)."""
        try:
            r = httpx.post(f"{self.base_url}/api/switch",
                           headers=self._headers(),
                           json={"model": name, "ctx": ctx, "ngl": ngl},
                           timeout=self.timeout)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                hint = ("no token configured" if not self.token else
                         "token rejected — may be stale")
                raise LlamaDeskError(
                    f"LlamaDesk switch: 401 unauthorized ({hint}). "
                    f"Set llamadesk.token_env in config.yaml and run "
                    f"`aurora key set <ENV_VAR>` with a valid token.") from e
            raise LlamaDeskError(f"LlamaDesk switch: {e}") from e
        except httpx.HTTPError as e:
            raise LlamaDeskError(f"LlamaDesk switch: {e}") from e

    def wait_ready(self, name: str, poll: float = 3.0, timeout: float = 240,
                   on_tick=None) -> bool:
        """Poll status until `name` is loaded (loads take ~1-2 min)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.loaded_model() == name and not self.busy():
                    return True
            except LlamaDeskError:
                pass  # server restarting mid-load is normal
            if on_tick:
                on_tick()
            time.sleep(poll)
        return False
