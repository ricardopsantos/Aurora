"""`/context` — a session rendered as a tree (R134).

A pure READ over the session JSONL. Every number here was already written by
`Session.log()` (R20, R92, and R133's additions); nothing new is accounted
for and nothing is kept in sync, so this works on sessions that ended long
ago — subject to what was recorded at the time (see `_MISSING`).

UI-side on purpose: it colours, so it may not be imported by `engine.py`
(R25 boundary, enforced by `test_architecture.py`). Both front ends reach it
through `ui._handle_command`.
"""

import math

from . import session as sessions
from .colors import BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW, dim
from .tokens import fmt_token_count

# how many turns render by default — a long session is unbounded (nothing is
# ever auto-deleted, R20) and the chat area is not a pager
DEFAULT_TURNS = 20

# a turn label is a reminder, not the prompt
_LABEL_CHARS = 40

# R133: only sessions logged after it carry these. A reader must treat their
# absence as "not recorded" — NEVER as "it didn't happen". Pre-R133 sessions
# have no approval records at all, so an empty approval list there says
# nothing about whether the user was asked.
_MISSING = "· some fields predate R133 and were not recorded"

_STATUS_GLYPH = {"ok": f"{GREEN}✓{RESET}", "error": f"{RED}✗{RESET}",
                 "skipped": f"{DIM}⊘{RESET}"}

_DECISION_GLYPH = {"approved": "✅", "always_allow": "✅", "allowlisted": "✅",
                   "denied": "⛔", "denied_policy": "⛔", "always_deny": "⛔",
                   "steered": "✎", "stopped": "⏹"}


class _Turn:
    def __init__(self, label: str, model: str):
        self.label, self.model = label, model
        self.rows: list[tuple] = []      # (kind, name, status, decision)
        self.stats: dict | None = None   # None = never completed
        self.pending_approval: str | None = None


def _flush_approval(turn: "_Turn | None") -> None:
    """Emit a still-pending approval as its own orphan row.

    An approval is held back so it can be merged onto its tool's row, but
    every path that ENDS the turn without that tool record arriving has to
    let it go, or the decision vanishes from the tree entirely (R138i fixed
    exactly this for a second approval arriving first). "The user was asked
    and answered" is never something this view may silently drop."""
    if turn is not None and turn.pending_approval:
        turn.rows.append(("approval", turn.pending_approval[0], None,
                          turn.pending_approval[1]))
        turn.pending_approval = None


def _collect(session_id: str) -> tuple[list, bool]:
    """Walk one session's records into an ordered list of `_Turn`s and
    session-level markers (`/compact`, `/model`). Returns (rows, legacy)."""
    rows: list = []
    turn: _Turn | None = None
    legacy = False
    # R96e: name the events instead of parsing every line. A session's log is
    # dominated by `tool` records (often several KB each) and carries plenty
    # this view never reads — /clear, /reset, remember, bootstrap, model
    # add/remove — which are now skipped without a json.loads.
    wanted = {"user", "assistant", "tool", "approval", "compact",
              "model_switch", "clear", "reset"}
    for r in sessions.Session(session_id).iter_records(events=wanted):
        ev = r.get("event")
        if ev == "user":
            text = " ".join((r.get("text") or "").split())
            if len(text) > _LABEL_CHARS:
                text = text[:_LABEL_CHARS - 1] + "…"
            turn = _Turn(text, r.get("model") or "?")
            rows.append(turn)
        elif ev == "approval" and turn is not None:
            # carries the tool NAME so it can only ever attach to that tool's
            # result. Position alone would be enough today, but a gate outcome
            # that never produces a tool record (`stopped` — the remaining
            # calls are answered internally and never reach on_tool_result)
            # would then drift onto whatever row came next.
            if turn.pending_approval:
                # a SECOND approval arrived before the first one's tool
                # record did — e.g. a gate that answers two calls in a row
                # with `denied_policy` then `stopped`, neither producing an
                # on_tool_result. An unconditional overwrite here silently
                # dropped the first decision entirely; flush it as its own
                # orphan row instead (review pass, R134-adjacent).
                turn.rows.append(("approval", turn.pending_approval[0], None,
                                  turn.pending_approval[1]))
            turn.pending_approval = (r.get("tool") or "",
                                     r.get("decision") or "")
        elif ev == "tool" and turn is not None:
            status = r.get("status")
            if status is None:
                legacy = True          # pre-R133: outcome was never written
            name = r.get("name") or "?"
            pending = turn.pending_approval
            if pending and pending[0] and pending[0] != name:
                # the pending approval belongs to a call that produced no
                # result of its own — keep it as its own row rather than
                # mislabelling this one
                turn.rows.append(("approval", pending[0], None, pending[1]))
                pending = None
            turn.rows.append(("tool", name, status,
                              pending[1] if pending else None))
            turn.pending_approval = None
        elif ev == "assistant" and turn is not None:
            _flush_approval(turn)          # e.g. `stopped`: no tool followed
            turn.stats = r
            if r.get("reasoning_tokens") is None:
                legacy = True
            turn = None
        elif ev == "compact":
            _flush_approval(turn)
            rows.append(("compact", r.get("folded") or 0,
                         r.get("used_before"), r.get("used_after")))
            turn = None
        elif ev == "model_switch":
            _flush_approval(turn)
            rows.append(("model", r.get("model") or "?"))
        elif ev in ("clear", "reset"):
            # bug fix (2026-07-27): /clear (and /reset, which calls it)
            # were EXCLUDED from `wanted` entirely — R96e's prefilter
            # skipped them without a json.loads, so the running `ctx`
            # figure computed below never saw one and kept reporting the
            # LAST turn's stale context size even though the live engine
            # (and the status bar) had genuinely dropped to zero. `engine.
            # clear()` always logs "clear"; `engine.reset()` calls that
            # AND logs its own "reset" right after, so a /reset shows both
            # markers — two things really did happen (history dropped,
            # system prompt reset too).
            _flush_approval(turn)
            rows.append((ev,))
            turn = None
    # the log can simply STOP with an approval still pending — the process was
    # killed between the approval record and its tool record (a session log is
    # append-only, so whatever was written survives). Without this the tree
    # renders that session one row short, silently.
    _flush_approval(turn)
    return rows, legacy


def _reasoning(stats: dict) -> tuple[str, bool]:
    """(THINK: text, is_subset_of_output).

    A REPORTED `reasoning_tokens` is documented to be part of
    `completion_tokens`, so it renders inside `OUT:` as "of it". An ESTIMATE
    from streamed characters is NOT: nothing says a backend that declines to
    count reasoning also folded it into its completion total, and claiming
    otherwise produces the visibly impossible "OUT: 900 (THINK: ~12k of
    it)". The estimate therefore stands beside `OUT:` as its own term,
    never inside it."""
    real = int(stats.get("reasoning_tokens") or 0)
    if real:
        return fmt_token_count(real), True
    chars = int(stats.get("reasoning_chars") or 0)
    if chars:
        # tokens.estimate_tokens' own ~4-chars/token rule, applied to a count
        # instead of a string — materialising the text just to measure it
        # would be O(the whole reasoning stream) for one display number
        return "~" + fmt_token_count(chars // 4), False
    return "0", True


def _badges(stats: dict) -> str:
    from .providers.openai_compat import cost_for
    inp = int(stats.get("input_tokens") or 0)
    billed = int(stats.get("billed_input") or inp)
    out = int(stats.get("output_tokens") or 0)
    cached = int(stats.get("cached_input") or 0)

    # `IN:` names the two numbers instead of an arrow (feature request,
    # 2026-07-27): "8.5k→25.7k billed" reads as growth ("input went from X
    # to Y"), but `billed` is a SUM across every round of a multi-tool turn
    # (R37 — each round re-pays for the whole, growing prompt), not a
    # single later value. Spelling out "last"/"billed" removes that
    # ambiguity; only shown at all when they differ (a single-round turn
    # has nothing to disambiguate).
    # `fmt_token_count` drops the unit entirely below 1000 (e.g. "845" —
    # no "k", nothing) — a bare number with no label anywhere on the line
    # answering "845 WHAT?". "tok" is spelled out once on each clause's
    # LEADING figure (not repeated on "billed"/"last", which are already
    # self-explanatory once the unit is established) so it reads as a
    # real measurement, not a mystery count.
    box = f"{fmt_token_count(inp)} tok"
    if billed != inp:
        box = f"{box} last · {fmt_token_count(billed)} billed"
    # each badge gets its own colour (feature request, 2026-07-27) — IN/
    # OUT/TOOLS/CACHE used to render in one uniform (terminal-default)
    # colour, which made a dense multi-tool turn's line a wall of
    # same-weight text to scan. Picked for eye comfort over vividness:
    # CYAN (in)/MAGENTA (out) are a standard, easily-told-apart pair;
    # YELLOW (tools) already reads as "activity" elsewhere in the UI;
    # GREEN (cache) was already this colour — kept, since green already
    # means "this saved you money" everywhere else Aurora uses it. Each
    # segment is self-contained ({X}...{RESET}), never nested inside
    # another colour's span, so ANSI's lack of a colour stack can't leak
    # one segment's colour into the next.
    in_part = f"{CYAN}IN: {box}{RESET}"
    parts = [in_part]
    # `CACHE:` used to be its own trailing badge, disconnected from the
    # `billed` figure it's a property OF; briefly folded INTO the `IN`
    # clause with a comma, which read as part of the same number instead
    # of its own fact. Its own `│`-separated segment right after `IN`
    # keeps it adjacent (still visually "about billed", not the far-end
    # afterthought it used to be) without blurring into that figure. Same
    # 10% floor and 100% clamp as before (a pre-R92 log can report
    # `cached` against a different, smaller denominator than `billed`,
    # producing a nonsensical >100% ratio; that's a display artifact of
    # old data, not a real discount over 100%).
    if billed and cached / billed >= 0.10:
        pct = min(100, round(cached / billed * 100))
        parts.append(f"{GREEN}{pct}% cached{RESET}")

    # `THINK:` is a SUBSET of `OUT:` when reported, never additive (R133a) —
    # the label says "of it" rather than sitting alongside as an independent
    # figure, because side by side reads as a sum and a thinking turn would
    # look twice as expensive as it was. Omitted entirely at zero (feature
    # request) — every non-reasoning-model turn used to carry a permanent,
    # always-zero "(THINK: 0 of it)" for no reason.
    think, inside = _reasoning(stats)
    if inside and think != "0":
        parts.append(f"{MAGENTA}OUT: {fmt_token_count(out)} tok (THINK: {think} of it){RESET}")
    elif inside:
        parts.append(f"{MAGENTA}OUT: {fmt_token_count(out)} tok{RESET}")
    else:
        parts += [f"{MAGENTA}THINK: {think} tok{RESET}",
                  f"{MAGENTA}OUT: {fmt_token_count(out)} tok{RESET}"]
    parts.append(f"{YELLOW}TOOLS: {stats.get('_tools', 0)}{RESET}")

    # R203: `cached` included — this badge and the status bar are describing
    # the same turn and must not disagree (they did, by ~7x on a cache-heavy
    # session).
    usd = cost_for(stats.get("model") or "", billed, out, cached)
    if usd is not None:
        parts.append(f"${usd:,.4f}".rstrip("0").rstrip("."))
    return " │ ".join(parts)


def _runs(tool_rows: list) -> list[tuple]:
    """Collapse CONSECUTIVE identical rows into (row, count) — R134h.

    A real bootstrap turn opens with a dozen `read_file` calls and fills the
    whole tree with one repeated word, burying the rows that differ. Identity
    is the full row (name, status AND approval decision), so a failure is
    never absorbed into a run of successes and an approved call never merges
    with a denied one — the outliers are exactly what the reader is scanning
    for. Only ADJACENT rows merge: a single `✗` in the middle splits a run of
    ten into 3 and 7, which is more informative than `(x10)`, not less."""
    out: list[tuple] = []
    for row in tool_rows:
        if out and out[-1][0] == row:
            out[-1] = (row, out[-1][1] + 1)
        else:
            out.append((row, 1))
    return out


def _session_cost(turns: list) -> tuple[float, str]:
    """(total $, 'all' | 'some' | '').

    R203: this used to document a deliberate UPPER bound — "cached reads bill
    cheaper but the discount isn't reported uniformly, so nothing is
    subtracted". R192 retired that premise: `cached_input` is on every
    assistant record, and the rate reconciled to the cent against a real
    OpenRouter invoice. Kept as a note rather than deleted because the old
    reasoning was sound when written; it is the FACTS that changed."""
    from .providers.openai_compat import cost_for
    total, seen, missing = 0.0, False, False
    for t in turns:
        if not t.stats:
            continue
        usd = cost_for(t.stats.get("model") or "",
                       int(t.stats.get("billed_input")
                           or t.stats.get("input_tokens") or 0),
                       int(t.stats.get("output_tokens") or 0),
                       int(t.stats.get("cached_input") or 0))
        if usd is None:
            missing = True
            continue
        seen = True
        total += usd
    return total, ("" if not seen else "some" if missing else "all")


def model_breakdown_lines(usage_rows: dict) -> tuple[list[str], float]:
    """Per-model `turns · in · out · $ · cached` rows, `usage_rows` shaped
    like `session.usage_by_model()`/`usage_all_sessions()` (R166). Shared by
    `/cost` (which always reads the cross-session aggregate now, R166) and
    `/context <id>` (per-session tree, only worth printing when a session
    spans more than one model — e.g. `/fallback`, R162, or a manual
    `/model` switch mid-session). Returns (lines, priced total $)."""
    from .providers.openai_compat import cost_for
    out, total = [], 0.0
    for model in sorted(usage_rows):
        r = usage_rows[model]
        # R203: `usage_by_model` already sums `cached` per model, so the
        # breakdown can price it properly instead of ignoring the column it
        # prints two fields later.
        usd = cost_for(model, r["billed"], r["output"], r.get("cached", 0))
        if usd is not None:
            total += usd
            money = f"${usd:,.4f}".rstrip("0").rstrip(".")
        else:
            money = dim("no price")
        cached = f"  {GREEN}{fmt_token_count(r['cached'])} cached{RESET}" \
            if r["cached"] else ""
        out.append(f"  {BOLD}{model}{RESET}")
        out.append(f"    {r['turns']} turn{'s' if r['turns'] != 1 else ''}"
                   f" · in {fmt_token_count(r['billed'])}"
                   f" · out {fmt_token_count(r['output'])}"
                   f" · {money}{cached}")
    return out, total


def _projection(turns: list, ctx: int, limit: int, total_usd: float,
                priced: str) -> str:
    """Feature request (2026-07-27): "at this rate, this session hits $X /
    100% ctx in N turns" — a linear extrapolation off this session's OWN
    turn-over-turn trend, the natural extension of R163's live `$` badge
    from "here's the number now" to "here's where it's headed". Every input
    already exists (per-turn `input_tokens` IS the running context size,
    already summed for `total_usd`/`ctx` above) — this reads the trend, it
    doesn't track anything new.

    Needs at least 3 priced turns to call it a trend rather than noise, and
    the context growth must be positive — a session that's been flat (all
    cache hits, or already past a /compact fold with no growth since)
    has no "fills up" point to project, so this returns "" rather than a
    misleading N."""
    priced_turns = [t for t in turns
                    if t.stats and t.stats.get("input_tokens") is not None]
    if len(priced_turns) < 3 or limit <= 0:
        return ""
    ctxs = [int(t.stats.get("input_tokens") or 0) for t in priced_turns]
    growth = (ctxs[-1] - ctxs[0]) / (len(ctxs) - 1)
    remaining = limit - ctx
    if growth <= 0 or remaining <= 0:
        return ""
    turns_left = math.ceil(remaining / growth)
    line = f"· at this rate: ~{turns_left} more turn(s) until context fills"
    if priced:
        avg_cost = total_usd / len(priced_turns)
        projected = total_usd + avg_cost * turns_left
        money = f"${projected:,.2f}"
        line += f" (~{money} total by then{'' if priced == 'all' else ', some models unpriced'})"
    return line


def render(session_id: str, limit: int | None = DEFAULT_TURNS,
          context_limit: int | None = None) -> str:
    """The tree for one session. `limit=None` renders every turn.
    `context_limit` (the model's live ctx window, from
    `engine.context_stats().limit`) enables the "at this rate…" projection
    line — only known for the CURRENT session, so `report()` passes it only
    then; a past/other session's tree renders without it, same as before."""
    rows, legacy = _collect(session_id)
    if not rows:
        return dim(f"· session {session_id} has nothing logged yet")

    turns = [r for r in rows if isinstance(r, _Turn)]
    numbers = {id(t): i + 1 for i, t in enumerate(turns)}
    shown = rows
    if limit is not None and len(turns) > limit:
        cut = rows.index(turns[-limit])
        shown = rows[cut:]

    total_in = sum(int(t.stats.get("billed_input") or
                       t.stats.get("input_tokens") or 0)
                   for t in turns if t.stats)
    # the LAST context figure in record order — which may come from a fold,
    # not a turn. Reading only turns reported the pre-fold size for any
    # session that ended on /compact, i.e. overstated it by the whole drop.
    ctx = 0
    for r in rows:
        if isinstance(r, _Turn):
            if r.stats:
                ctx = int(r.stats.get("input_tokens") or 0)
        elif r[0] == "compact" and r[3] is not None:
            ctx = int(r[3])
        elif r[0] in ("clear", "reset"):
            # bug fix (2026-07-27): /clear drops the live engine's context
            # to zero (`engine.clear()`'s `self._used = 0`) — this must
            # too, or /context kept reporting the pre-clear turn's stale
            # size (e.g. "ctx 15.5k" right after a /clear that emptied it,
            # while the status bar correctly showed "ctx 0").
            ctx = 0

    head = (f"{BOLD}session/{session_id}{RESET} · "
            f"{len(turns)} turn{'s' if len(turns) != 1 else ''} · "
            f"ctx {fmt_token_count(ctx)} · in {fmt_token_count(total_in)}")
    total_usd, priced = _session_cost(turns)
    if priced:
        money = f"${total_usd:,.4f}".rstrip("0").rstrip(".")
        # `+` when some turn ran on a model with no price entry: the total is
        # then a floor, and a bare figure would read as the whole bill
        head += f" · {money}{'' if priced == 'all' else '+'}"
    if shown is not rows:
        head += dim(f" · showing last {limit}")
    out = [head]
    if context_limit:
        proj = _projection(turns, ctx, context_limit, total_usd, priced)
        if proj:
            out.append(dim(proj))

    # R166: a per-model breakdown only earns its place when the session
    # actually used more than one — the single-model case is already fully
    # covered by `head`'s ctx/in/$ figures, and repeating them under a model
    # name nobody switched away from would just be noise.
    usage = sessions.usage_by_model(session_id)
    if len(usage) > 1:
        lines, _ = model_breakdown_lines(usage)
        out.extend(lines)

    for i, row in enumerate(shown):
        last = i == len(shown) - 1
        stem = "└──" if last else "├──"
        cont = "   " if last else "│  "
        if not isinstance(row, _Turn):
            if row[0] == "compact":
                _, folded, was, now = row
                # the DROP is the point of a fold; without it the spine just
                # stops descending for no visible reason (pre-R134a logs have
                # no before/after, so they still show the count alone)
                drop = ""
                if was is not None and now is not None:
                    drop = (f" · {fmt_token_count(int(was))} → "
                            f"{fmt_token_count(int(now))}")
                out.append(f"{stem} {YELLOW}/compact{RESET} ── "
                           f"folded {folded} messages{drop}")
            elif row[0] in ("clear", "reset"):
                out.append(f"{stem} {YELLOW}/{row[0]}{RESET} ── "
                           f"context dropped to 0")
            else:
                out.append(f"{stem} {YELLOW}/model{RESET} → {row[1]}")
            continue

        label = f' · "{row.label}"' if row.label else ""
        # numbered against the WHOLE session, not the visible slice — with
        # `showing last 20` the numbers have to still mean something
        out.append(f"{stem} {BOLD}Turn {numbers[id(row)]}{RESET}{label}")
        if row.stats is None:
            out.append(f"{cont} {dim('(no reply logged — the turn produced nothing)')}")
        else:
            stats = dict(row.stats)
            stats["_tools"] = sum(1 for r in row.rows if r[0] == "tool")
            out.append(f"{cont} {_badges(stats)}")
        grouped = _runs(row.rows)
        for j, ((kind, name, status, decision), n) in enumerate(grouped):
            tip = "└──" if j == len(grouped) - 1 else "├──"
            glyph = _STATUS_GLYPH.get(status, dim("?"))
            if n > 1:
                name = f"{name} {DIM}(x{n}){RESET}"
            if kind == "approval":
                # a gate outcome with no tool result behind it (`stopped`):
                # the call never ran, so there is no status to show
                mark = _DECISION_GLYPH.get(decision, "?")
                out.append(f"{cont} {tip} {DIM}[approval]{RESET} {name} "
                           f"→ {mark}")
                continue
            # an approval that was ASKED gets its own marker; an allowlisted
            # pass does not — it was silent, and a ✅ on every write would
            # drown the ones the user actually answered
            if decision and decision != "allowlisted":
                mark = _DECISION_GLYPH.get(decision, "?")
                # a refusal already says the call never ran — repeating the
                # ⊘ next to ⛔ is noise. An APPROVED call that then failed is
                # the opposite: "you said yes and it broke" is the whole
                # point, so that keeps its ✗.
                tail = f" {glyph}" if status == "error" else ""
                out.append(f"{cont} {tip} {DIM}[approval]{RESET} {name} "
                           f"→ {mark}{tail}")
            else:
                out.append(f"{cont} {tip} {name} {glyph}")

    if legacy:
        out.append(dim(_MISSING))
    return "\n".join(out)


def report(engine, arg: str) -> str:
    """`/context [all|<n>] [<session-id>]`."""
    limit = DEFAULT_TURNS
    session_id = engine.session.id
    for w in arg.split():
        if w.lower() == "all":
            limit = None
        elif w.isdigit() and int(w):
            # a bare number is a turn count, not a session id — session ids
            # are 12 hex chars, so this can't shadow a real one
            limit = int(w)
        else:
            session_id = w
    if session_id != engine.session.id and \
            not sessions.Session(session_id).log_path.exists():
        # distinct from "logged nothing yet": a typo'd id must not look like
        # a session that simply has no turns
        return dim(f"· no session {session_id} on this machine "
                   "(/resume lists them)")
    context_limit = None
    if session_id == engine.session.id and hasattr(engine, "context_stats"):
        context_limit = engine.context_stats().limit
    return render(session_id, limit, context_limit=context_limit)
