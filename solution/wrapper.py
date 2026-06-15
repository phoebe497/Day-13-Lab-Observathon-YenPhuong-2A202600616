"""YOUR mitigation + observability layer. The simulator calls mitigate() around the
opaque agent (a REAL LLM) for every request. This is the ONLY place observability can
live -- the agent is silent. Legal moves: retry / cache / route / guardrail / sanitize
/ fallback / session-reset / PROMPT ROUTING, plus your own logging/tracing/metrics.
Illegal: hardcoding answers, importing the agent internals, reading instructor files,
network exfiltration.

  call_next(question, config) -> result   # the only way to reach the black box
  context = {"session_id","turn_index","qid","cache": <shared dict>, "cache_lock": <Lock>}
  result  = {"answer","status","steps","trace","meta":{latency_ms,usage,...}}

PROMPT ROUTING: you can override the agent's system prompt PER REQUEST by setting it in
the config you pass to call_next, e.g.:
    conf = dict(config); conf["system_prompt"] = my_better_prompt
    result = call_next(question, conf)
(Or just edit solution/prompt.txt for a single static prompt used on every request.)
"""
from __future__ import annotations
import time
import re

from telemetry.logger import logger, new_correlation_id, set_correlation_id
from telemetry.cost import cost_from_usage
from telemetry.redact import redact


# ─── Injection patterns that signal an adversarial note ────────────────────────
_INJECT_PATTERNS = [
    # Instruction verbs targeting pricing/discounts
    re.compile(r'(?i)\b(ap\s*dung|ap\s*d[uụ]ng|áp\s*d[uụ]ng)\b.*?(gi[aá]|discount|coupon|%)', re.DOTALL),
    re.compile(r'(?i)\b(mi[eê]n\s*ph[ií]|free\s+ship|mien\s*phi)\b'),
    re.compile(r'(?i)\b(gi[aá]\s*m[aã]c|gi[aá]\s*g[oô]c)\s*[=:]\s*\d'),
    re.compile(r'(?i)\b(t[oô]ng\s*ti[eề]n|total)\s*[=:]\s*\d'),
    re.compile(r'(?i)\b(b[oỏ]\s*qua|bo\s*qua|ignore|disregard)\b'),
    re.compile(r'(?i)\b(system\s*prompt|system_prompt|forget|override)\b'),
]

# ─── Note section detector ─────────────────────────────────────────────────────
_NOTE_PAT = re.compile(
    r'(?i)(ghi\s*ch[úu]|ghi\s*chu|note\s*[:：]|order\s*note|gchu\b)(.*?)(?=$|\n)',
    re.DOTALL
)


def _is_injected_note(note_content: str) -> bool:
    """Return True if the note looks like a prompt-injection attempt."""
    return any(p.search(note_content) for p in _INJECT_PATTERNS)


def sanitize_question(q: str) -> str:
    """Neutralise injection in Ghi Chú / Note sections only.
    
    Strategy: detect the note section, check if it contains injection patterns.
    If yes, replace the note content with a safe label. If clean, leave it alone.
    This avoids destroying legitimate quantity/coupon info that precedes the note.
    """
    if not isinstance(q, str):
        return q

    def _handle_note(match):
        prefix = match.group(1)
        content = match.group(2)
        if _is_injected_note(content):
            if logger:
                logger.log_event("INJECTION_BLOCKED", {
                    "note_preview": content[:80]
                })
            return f"{prefix}: [NOTE_REDACTED]"
        return match.group(0)  # keep note intact if it's benign

    return _NOTE_PAT.sub(_handle_note, q)


def mitigate(call_next, question, config, context):
    # Set correlation ID for tracing
    cid = context.get("qid") or new_correlation_id()
    set_correlation_id(cid)

    # 1. Caching (keyed by session + exact question)
    cache_key = (context.get("session_id"), question)
    with context["cache_lock"]:
        if cache_key in context["cache"]:
            if logger:
                logger.log_event("CACHE_HIT", {
                    "qid": context.get("qid"),
                    "session_id": context.get("session_id"),
                })
            return context["cache"][cache_key]

    # 2. Input sanitisation – neutralise injection in notes only
    sanitized_q = sanitize_question(question)
    if sanitized_q != question and logger:
        logger.log_event("QUESTION_SANITIZED", {"qid": context.get("qid")})

    # 3. Call agent with retry + backoff
    max_attempts = 5
    res = None
    t_start = time.time()

    for attempt in range(1, max_attempts + 1):
        t0 = time.time()
        try:
            res = call_next(sanitized_q, config)
            wall_ms = int((time.time() - t0) * 1000)

            status = res.get("status", "error")
            if status == "ok" or attempt == max_attempts:
                break

            # Non-ok status (loop, max_steps) → retry
            if logger:
                logger.log_event("RETRY_TRIGGERED", {
                    "qid": context.get("qid"),
                    "attempt": attempt,
                    "status": status,
                    "wall_ms": wall_ms,
                })
            time.sleep(0.3 * attempt)

        except Exception as e:
            err_msg = str(e)
            is_rate_limit = (
                "429" in err_msg
                or "rate_limit" in err_msg.lower()
                or "limit reached" in err_msg.lower()
            )

            if attempt == max_attempts:
                res = {
                    "answer": "Co loi he thong, vui long thu lai.",
                    "status": "wrapper_error",
                    "steps": 0,
                    "trace": [],
                    "meta": {"latency_ms": 0, "usage": {}, "tools_used": []},
                }
                break

            if is_rate_limit:
                m = re.search(
                    r'(?:try again in|retry after|in)\s*(\d+\.?\d*)\s*s',
                    err_msg, re.IGNORECASE
                )
                sleep_time = (float(m.group(1)) + 1.5) if m else (4.0 * attempt)
                if logger:
                    logger.log_event("RATE_LIMIT_BACKOFF", {
                        "qid": context.get("qid"),
                        "sleep_time": sleep_time,
                        "attempt": attempt,
                    })
                time.sleep(sleep_time)
            else:
                time.sleep(0.5 * attempt)

    total_wall_ms = int((time.time() - t_start) * 1000)

    # 4. PII redaction on the final answer
    redacted_count = 0
    if res and res.get("answer"):
        redacted_ans, redacted_count = redact(res["answer"])
        if redacted_count > 0:
            res["answer"] = redacted_ans
            if logger:
                logger.log_event("PII_REDACTED", {
                    "qid": context.get("qid"),
                    "redacted_count": redacted_count,
                })

    # 5. Populate cache
    with context["cache_lock"]:
        context["cache"][cache_key] = res

    # 6. Observability logging
    meta = res.get("meta", {}) if res else {}
    usage = meta.get("usage", {})
    model = meta.get("model", "")
    cost = cost_from_usage(model, usage)

    if logger:
        if res and res.get("status") == "loop":
            logger.log_event("LOOP_DETECTED", {
                "qid": context.get("qid"),
                "trace_len": len(res.get("trace", [])),
            })

        logger.log_event("AGENT_CALL", {
            "qid": context.get("qid"),
            "session_id": context.get("session_id"),
            "turn_index": context.get("turn_index"),
            "status": res.get("status") if res else "none",
            "steps": res.get("steps") if res else 0,
            "reported_latency_ms": meta.get("latency_ms"),
            "wall_ms": total_wall_ms,
            "tokens": usage,
            "cost_usd": cost,
            "tools_used": meta.get("tools_used", []),
            "pii_redacted": redacted_count > 0,
            "injected": sanitized_q != question,
        })

    return res
