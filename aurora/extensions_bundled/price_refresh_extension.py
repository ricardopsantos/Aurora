"""Bundled default extension (R136): a `refresh_model_prices` tool that
re-pulls price/context-size info from OpenRouter's public catalog for every
OpenRouter model already listed in config.yaml's `models:` — the batch
counterpart to `/model add`'s automatic one-model lookup (`aurora/ui.py`
`_add_model_cmd`), for models added before pricing existed, or whose listed
price has since changed. The local model (no OpenRouter listing, no price)
and any model no longer on OpenRouter's catalog are skipped, not errors.

Dynamic `register(engine)` style (see `aurora/extensions.py`'s docstring):
the model list to refresh comes from `engine.cfg`/`engine.models`, so it
can't be a static SPEC/RUNNERS module like `lint_extension.py`. The tool
table below is named `_SPEC`, not `SPEC` — `discover()` merges a module's
top-level `SPEC`/`RUNNERS` *and* whatever `register()` returns (a file may
use both styles at once), so a plain `SPEC` here would have been picked up
twice: once as a runner-less static spec, once via `register()`, tripping
`tools.set_extensions()`'s duplicate-name guard.

This is a MODEL-CALLABLE tool, not an automatic hook — same posture as
`lint_check` (see EXTENSIONS.md's "what's scoped out": no lifecycle-hook
system yet). It runs when the model chooses to call it, nudged by the
tool's own description."""

_SPEC = [{
    "name": "refresh_model_prices",
    "description": (
        "Refresh price and context-size info for every OpenRouter model "
        "configured in config.yaml's models: list, pulling from "
        "OpenRouter's public catalog. Call this if a status-bar price "
        "looks stale, or is missing for a model that was added before "
        "pricing existed. Skips the local model (no price) and any model "
        "no longer listed on OpenRouter."),
    "parameters": {"type": "object", "properties": {}},
}]


def register(engine):
    def refresh_model_prices(**_):
        from ..providers.openai_compat import (
            refresh_prices_for,
            save_remote_model_infos,
        )
        # R136 review: de-duplicated (dict.fromkeys preserves first-seen
        # order) — two config.yaml entries for the same id used to print and
        # persist the model twice for no reason.
        model_ids = list(dict.fromkeys(
            m["model"] for m in engine.models
            if m.get("provider") == "openrouter" and m.get("model")))
        if not model_ids:
            return "no OpenRouter models configured in config.yaml"
        info_by_id, catalog_ok = refresh_prices_for(model_ids)
        if not catalog_ok:
            return ("couldn't reach the OpenRouter catalog "
                     "(openrouter.ai/api/v1/models) — try again later")
        # One batched read-modify-write for every model refreshed (see
        # save_remote_model_infos's docstring) instead of N single-model
        # ones — was N full file rewrites for N configured models.
        save_remote_model_infos({mid: info for mid, info in info_by_id.items()})
        lines = []
        for mid in model_ids:
            info = info_by_id.get(mid)
            if info is None:
                lines.append(f"{mid}: not found on OpenRouter (skipped)")
                continue
            ctx = info.get("context_size")
            pi, po = info.get("price_in_per_mtok"), info.get("price_out_per_mtok")
            bits = []
            # R136 review: guard the display-side int() too — malformed
            # catalog data must not abort the whole tool call, just this
            # one model's ctx bit (the price/name still get reported).
            if ctx is not None:
                try:
                    bits.append(f"ctx {int(ctx) // 1000}k")
                except (TypeError, ValueError):
                    pass
            if pi is not None and po is not None:
                bits.append(f"${pi:g}/${po:g} per M (listed price)")
            lines.append(f"{mid}: {', '.join(bits) if bits else 'no price/ctx listed'}")
        return "\n".join(lines)

    return _SPEC, {"refresh_model_prices": refresh_model_prices}
