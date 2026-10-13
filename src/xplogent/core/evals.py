"""Agent evaluations.

A *suite* is a set of cases ``{prompt, criteria}``. Running a suite executes each
prompt through a fresh, memory-free agent (so runs are reproducible and don't
pollute the user's memory), then grades the answer with an **LLM judge** — the
same model used for reflection. Results are persisted to ``eval_runs`` so the
dashboard can chart pass-rate over time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from xplogent.core.config import Config, load_config
from xplogent.providers.base import Message, Provider, Role
from xplogent.providers.registry import build_provider
from xplogent.runtime import build_runtime

_JUDGE_PROMPT = """You are a strict but fair evaluator of an AI agent's answer.
Given the user's PROMPT, the success CRITERIA, and the agent's ANSWER, decide
whether the answer satisfies the criteria.

Return ONLY a JSON object:
{"pass": true/false, "score": 0.0-1.0, "reason": "one short sentence"}
- "score" is your confidence the answer meets the criteria (1.0 = perfect).
- Do not wrap the JSON in markdown fences."""


@dataclass
class CaseResult:
    prompt: str
    criteria: str
    answer: str
    passed: bool
    score: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "prompt": self.prompt, "criteria": self.criteria, "answer": self.answer,
            "passed": self.passed, "score": self.score, "reason": self.reason,
        }


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


async def judge_answer(judge: Provider, prompt: str, criteria: str, answer: str) -> tuple[bool, float, str]:
    """Grade one answer with the LLM judge. Falls back to a substring check."""
    user = (f"PROMPT:\n{prompt}\n\nCRITERIA:\n{criteria or '(answer is helpful and correct)'}\n\n"
            f"ANSWER:\n{answer[:8000]}")
    try:
        reply = await judge.complete(
            [Message(role=Role.SYSTEM, content=_JUDGE_PROMPT),
             Message(role=Role.USER, content=user)],
            temperature=0.0,
        )
        data = _extract_json(reply.content)
    except Exception:  # noqa: BLE001 - judging must never crash a suite
        data = {}
    if isinstance(data, dict) and data:
        raw_pass = data.get("pass", False)
        # A judge that writes "false" as a string must not count as a pass.
        passed = (raw_pass.strip().lower() in ("true", "yes", "pass", "1")
                  if isinstance(raw_pass, str) else bool(raw_pass))
        try:
            score = float(data.get("score", 1.0 if passed else 0.0))
        except (TypeError, ValueError):  # e.g. "score": "high"
            score = 1.0 if passed else 0.0
        if score != score:  # NaN
            score = 0.0
        return passed, max(0.0, min(1.0, score)), str(data.get("reason", ""))
    # Heuristic fallback when the judge is unavailable: substring match on criteria.
    ok = bool(criteria) and criteria.lower() in answer.lower()
    return ok, 1.0 if ok else 0.0, "fallback substring check"


async def run_suite(eval_id: int, config: Config | None = None, *,
                    judge_model: str | None = None, model: str | None = None,
                    label: str = "") -> dict:
    """Run every case in a suite, judge it, persist an ``eval_runs`` row.

    ``model`` overrides the model used to run the cases (the judge is unchanged);
    ``label`` tags the recorded run (used to name A/B variants in history).
    """
    config = config or load_config()
    from xplogent.memory.store import Store

    store = Store(config.db_path)
    cases = store.eval_cases(eval_id)
    judge = build_provider(judge_model or config.reflection_model)
    results: list[CaseResult] = []
    try:
        for case in cases:
            rt = build_runtime(config, with_memory=False, model=model)
            try:
                answer = await rt.agent.run(case["prompt"])
            except Exception as exc:  # noqa: BLE001 - a bad case shouldn't abort the suite
                answer = f"(error: {exc})"
            finally:
                await rt.aclose()
            passed, score, reason = await judge_answer(
                judge, case["prompt"], case["criteria"], answer)
            results.append(CaseResult(case["prompt"], case["criteria"], answer,
                                      passed, score, reason))
    finally:
        await judge.aclose()

    n = len(results)
    passed = sum(1 for r in results if r.passed)
    avg = round(sum(r.score for r in results) / n, 3) if n else 0.0
    detail = json.dumps([r.as_dict() for r in results])
    run_model = model or config.model
    store.add_eval_run(eval_id, f"{label} · {run_model}" if label else run_model,
                       passed, n, avg, detail)
    store.close()
    return {"eval_id": eval_id, "passed": passed, "total": n, "score": avg,
            "model": run_model, "results": [r.as_dict() for r in results]}


async def run_ab(eval_id: int, variants: list[dict], config: Config | None = None, *,
                 judge_model: str | None = None) -> dict:
    """Run a suite under several prompt/model **variants** and pick a winner.

    Each variant is ``{name, system_prompt?, model?}``. The variant with the
    highest score (then most passes) wins; promote it with ``promote_variant``.
    """
    base = config or load_config()
    out: list[dict] = []
    for v in variants:
        overrides: dict = {}
        if v.get("system_prompt"):
            overrides["agent"] = {"system_prompt": v["system_prompt"]}
        vcfg = load_config(overrides=overrides) if overrides else base
        res = await run_suite(eval_id, vcfg, judge_model=judge_model,
                              model=v.get("model") or None, label=str(v.get("name", "")))
        out.append({"name": v.get("name", ""), "system_prompt": v.get("system_prompt", ""),
                    "model": res["model"], "passed": res["passed"],
                    "total": res["total"], "score": res["score"]})
    winner = max(out, key=lambda r: (r["score"], r["passed"]), default=None) if out else None
    return {"variants": out, "winner": winner["name"] if winner else None,
            "winner_detail": winner}


def promote_variant(system_prompt: str = "", model: str = "") -> dict:
    """Persist a winning variant's settings to user config (the live defaults)."""
    from xplogent.core.config import save_user_config

    updates: dict = {}
    if system_prompt:
        updates["agent"] = {"system_prompt": system_prompt}
    if model:
        updates["model"] = model
    if updates:
        save_user_config(updates)
    return {"ok": True, "promoted": list(updates.keys())}
