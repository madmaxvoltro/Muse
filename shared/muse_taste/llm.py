"""Optional LLM layer. Muse works fully without one; with one it gets better content
understanding and nicer explanations.

What it's used for (all cached, all background, never on the request path):
* **Item descriptors**: tone, themes and audience for each title ("bleak, slow-burn, dry humour").
  These go into the embedded document, the generated-item-profile idea from recent
  LLM-for-recsys work (e.g. "LLM-driven user and item profiles", ProMax 2026; Dai, Zhou &
  Joachims, "Language-Based User Profiles for Recommendation", 2024). Overviews say what
  happens; descriptors say what it *feels* like, which is what taste is about.
* **Interest labels**: "Slow-burn Nordic crime" instead of "cluster 3".
* **Taste summary**: a short natural-language description of each profile for the dashboard.

Providers (MUSE_LLM_PROVIDER):
  none       default
  openai     any OpenAI-compatible chat endpoint: Ollama (http://ollama:11434/v1), vLLM,
             LM Studio, llama.cpp, LocalAI, OpenRouter, OpenAI/ChatGPT.
             MUSE_LLM_BASE_URL, MUSE_LLM_API_KEY, MUSE_LLM_MODEL
  anthropic  Claude, through the official `anthropic` SDK. MUSE_LLM_API_KEY (or
             ANTHROPIC_API_KEY), MUSE_LLM_MODEL (default claude-opus-5-5).

Privacy: endpoints outside your network (OpenAI, Anthropic, OpenRouter...) are refused unless
MUSE_LLM_ALLOW_EXTERNAL=true. Item descriptors only send public metadata (title, overview,
keywords); interest labels and the taste summary send titles you watched, which is personal.
MUSE_LLM_PERSONAL_TASKS=false keeps those two local-only even when external is allowed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Protocol

from .privacy import require_private_or_allowed

logger = logging.getLogger("muse.llm")

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


class LLM(Protocol):
    name: str
    external: bool

    def complete_json(self, system: str, user: str, max_tokens: int = 600) -> dict[str, Any] | None: ...


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Tolerant: strips code fences and finds the first JSON object."""
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    depth = 0
    for i in range(start, len(t)) if start >= 0 else []:
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    v = json.loads(t[start : i + 1])
                    return v if isinstance(v, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, model: str, api_key: str | None, external: bool, timeout: float = 120.0):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.name = model
        self.external = external
        self._client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        self._json_mode = True

    def complete_json(self, system: str, user: str, max_tokens: int = 600) -> dict[str, Any] | None:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = self._client.post(f"{self.base_url}/chat/completions", json=body)
        if resp.status_code == 400 and self._json_mode:
            self._json_mode = False  # older servers reject response_format; the prompt asks for JSON anyway
            body.pop("response_format", None)
            resp = self._client.post(f"{self.base_url}/chat/completions", json=body)
        resp.raise_for_status()
        choice = resp.json()["choices"][0]["message"]
        return parse_json_object(choice.get("content") or "")


class AnthropicLLM:
    def __init__(self, model: str, api_key: str | None):
        import anthropic

        self.name = model
        self.external = True
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete_json(self, system: str, user: str, max_tokens: int = 600) -> dict[str, Any] | None:
        # Short, simple classification-style tasks: low effort. Server-side fallback means a
        # rare policy decline on the primary model is retried on a fallback model in the same
        # call instead of failing.
        response = self._client.beta.messages.create(
            model=self.name,
            max_tokens=max(max_tokens, 1024),
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            logger.info("LLM declined a request; skipping")
            return None
        text = "".join(b.text for b in response.content if b.type == "text")
        return parse_json_object(text)


def _secret_from(env: dict[str, str], name: str) -> str | None:
    """NAME_FILE (a Docker secret) wins over NAME."""
    path = env.get(f"{name}_FILE")
    if path:
        try:
            with open(path) as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None
    return env.get(name) or None


def from_env(env: dict[str, str] | None = None) -> LLM | None:
    env = dict(os.environ) if env is None else env
    provider = env.get("MUSE_LLM_PROVIDER", "none").lower()
    allow = env.get("MUSE_LLM_ALLOW_EXTERNAL", "false")
    if provider in ("none", "", "off"):
        return None
    if provider == "anthropic":
        require_private_or_allowed("https://api.anthropic.com", allow, what="LLM endpoint")
        return AnthropicLLM(env.get("MUSE_LLM_MODEL") or DEFAULT_ANTHROPIC_MODEL, _secret_from(env, "MUSE_LLM_API_KEY") or _secret_from(env, "ANTHROPIC_API_KEY"))
    if provider in ("openai", "ollama", "vllm", "lmstudio", "openrouter"):
        defaults = {"ollama": "http://ollama:11434/v1", "vllm": "http://vllm:8000/v1", "lmstudio": "http://lmstudio:1234/v1",
                    "openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1"}
        base = env.get("MUSE_LLM_BASE_URL") or defaults[provider]
        require_private_or_allowed(base, allow, what="LLM endpoint")
        from .privacy import is_private_host
        from urllib.parse import urlparse

        model = env.get("MUSE_LLM_MODEL") or ("qwen3:8b" if provider == "ollama" else "")
        if not model:
            raise ValueError("MUSE_LLM_MODEL is required for this provider")
        return OpenAICompatibleLLM(base, model, (_secret_from(env, "MUSE_LLM_API_KEY")), external=not is_private_host(urlparse(base).hostname or ""))
    raise ValueError(f"unknown MUSE_LLM_PROVIDER={provider!r}")


def personal_tasks_allowed(llm: LLM | None, env: dict[str, str] | None = None) -> bool:
    env = dict(os.environ) if env is None else env
    if llm is None:
        return False
    if not llm.external:
        return True
    return env.get("MUSE_LLM_PERSONAL_TASKS", "true").lower() in ("1", "true", "yes")


# ---- tasks ---------------------------------------------------------------------------------

ITEM_SYSTEM = (
    "You describe films, TV series, videos, music and books for a recommender system. "
    "Describe how the work feels to experience, not its plot. Reply with one JSON object only."
)


def describe_item(llm: LLM, fields: dict) -> dict | None:
    """-> {"tone": [...], "themes": [...], "audience": "..."}"""
    facts = {k: fields.get(k) for k in ("title", "year", "type", "genres", "keywords", "overview", "tagline", "directors", "artist", "channel", "author") if fields.get(k)}
    user = (
        "Work:\n" + json.dumps(facts, ensure_ascii=False)[:3500] + "\n\n"
        'Return {"tone": [3-6 short descriptors of mood/pacing/style, e.g. "slow-burn", "bleak", "whimsical", "dry humour"], '
        '"themes": [3-6 short themes, e.g. "grief", "found family", "class conflict"], '
        '"audience": "one short phrase, e.g. adults who like cerebral sci-fi"}. '
        "If you don't recognise the work, infer only from the facts given."
    )
    out = llm.complete_json(ITEM_SYSTEM, user, max_tokens=300)
    if not out:
        return None
    clean = {
        "tone": [str(x)[:40] for x in (out.get("tone") or [])][:6],
        "themes": [str(x)[:40] for x in (out.get("themes") or [])][:6],
        "audience": str(out.get("audience") or "")[:120] or None,
    }
    return clean if clean["tone"] or clean["themes"] else None


def label_interest(llm: LLM, titles: list[str], tags: list[str]) -> str | None:
    user = (
        "These titles form one cluster of someone's taste:\n- " + "\n- ".join(titles[:12]) +
        ("\nCommon tags: " + ", ".join(tags[:8]) if tags else "") +
        '\n\nName the shared taste in 2-5 words, specific and evocative (e.g. "Slow-burn Nordic crime", '
        '"Cosy Ghibli-style fantasy", "Mind-bending sci-fi"). Return {"label": "..."}.'
    )
    out = llm.complete_json("You name clusters of media taste. Reply with one JSON object only.", user, max_tokens=60)
    label = (out or {}).get("label")
    return str(label).strip()[:60] if label else None


def summarize_taste(llm: LLM, interest_labels: list[tuple[str, float]], loved: list[str], disliked: list[str]) -> str | None:
    user = (
        "Interests with share of attention:\n" + "\n".join(f"- {l} ({p:.0%})" for l, p in interest_labels[:10]) +
        "\nMost loved: " + ", ".join(loved[:12]) +
        ("\nDisliked: " + ", ".join(disliked[:8]) if disliked else "") +
        '\n\nWrite a warm, specific 2-3 sentence description of this person\'s taste in second person ("You..."). '
        'Return {"summary": "..."}.'
    )
    out = llm.complete_json("You describe media taste profiles. Reply with one JSON object only.", user, max_tokens=250)
    s = (out or {}).get("summary")
    return str(s).strip()[:600] if s else None


def narrate_evolution(llm: LLM, profile: Any, titles: dict[str, str]) -> str | None:
    """2-3 sentences on how this person's taste has been changing (from evolution.analyze)."""
    ev = profile.evolution or {}

    def name(k: str) -> str:
        it = profile.interests[int(k)]
        return it.label or ", ".join(t.split(":", 1)[-1] for t in it.top_tags[:2]) or titles.get(it.medoid_key, "")

    states = [f"- {name(k)}: {v.get('state')} (trend {v.get('trend', 0):+.2f}, since {v.get('first_seen')})" for k, v in ev.get("interests", {}).items()]
    miles = [f"- {m['month']}: {m['text']}" for m in ev.get("milestones", [])][-12:]
    user = (
        "Interests and how they are moving:\n" + "\n".join(states) + "\n\nMilestones:\n" + "\n".join(miles) +
        '\n\nIn 2-3 warm, specific sentences in second person, describe how this person\'s taste has been evolving '
        'and where it seems to be heading. No lists. Return {"narrative": "..."}.'
    )
    out = llm.complete_json("You describe how someone's media taste changes over time. Reply with one JSON object only.", user, max_tokens=300)
    n = (out or {}).get("narrative")
    return str(n).strip()[:700] if n else None
