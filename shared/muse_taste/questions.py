"""Which questions are worth asking, as queue entries.

Muse never depends on answers. The worker puts questions in a queue (question_log), any
client *may* fetch and answer them (API, MCP server), and unanswered questions expire without
consequence. Each entry has a priority (how much an answer would change recommendations), a
time to live, and a re-ask window so the same thing isn't asked again for months.

Kinds:
  onboarding      new profile with little evidence: tap the titles you love
  abandon         a title stopped early with no known reason: why?
  interest        taste evolution checks about a whole interest:
                    emerging   "You seem to be getting into X. More of that?"   more | keep | less
                    satiated   "You've watched a lot of X lately. Break?"       break | no_break
                    dormant    "You haven't watched X in a while. Bring it back?" bring_back | moved_on
  this_or_that    two plausible, uncertain, influential, different titles
  tag             critiquing: "How do you feel about films by X?" for an attribute common among your
                  best candidates that Muse knows little about

An answer to an interest question becomes an `interest_feedback` event; evolution.analyze turns
it into a decaying steer on that interest. An answer to any other question becomes the same
events the rest of Muse already understands (ratings, this-or-that picks, abandon reasons).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .elicitation import ABANDON_REASONS
from .profile import TasteProfile

MAX_PENDING_PER_USER = 12

INTEREST_ANSWERS = {
    "emerging": [("more", "Yes, more of that"), ("keep", "It's fine as it is"), ("less", "Less, please")],
    "satiated": [("break", "Yes, give me a break from it"), ("no_break", "No, keep it coming")],
    "dormant": [("bring_back", "Yes, bring it back"), ("moved_on", "No, I've moved on")],
}
INTEREST_PROMPTS = {
    "emerging": "You seem to be getting into {name}. More of that?",
    "satiated": "You've watched a lot of {name} lately. Want a break from it?",
    "dormant": "You haven't watched {name} in a while. Bring it back now and then?",
}


@dataclass
class QuestionSpec:
    kind: str
    domain: str
    dedupe_key: str
    priority: float  # 0..1: how much an answer would change recommendations
    ttl_days: int  # expires unanswered after this
    reask_days: int  # don't enqueue the same dedupe_key again within this window
    payload: dict[str, Any] = field(default_factory=dict)


def interest_name(profile: TasteProfile, k: int, titles: dict[str, str]) -> str:
    it = profile.interests[k]
    return it.label or ", ".join(t.split(":", 1)[-1] for t in it.top_tags[:2]) or titles.get(it.medoid_key, f"interest {k + 1}")


def interest_questions(profile: TasteProfile, titles: dict[str, str]) -> list[QuestionSpec]:
    ev = profile.evolution or {}
    out: list[QuestionSpec] = []
    for ks, info in (ev.get("interests") or {}).items():
        k = int(ks)
        if k >= len(profile.interests):
            continue
        it = profile.interests[k]
        if it.kind != "behaviour" or it.size < 3:
            continue
        sub = None
        if info.get("satiation", 0) >= 0.5:
            sub, prio, ttl = "satiated", 0.55 + 0.2 * (info["satiation"] - 0.5), 10
        elif info.get("state") in ("emerging", "growing") and info.get("trend", 0) > 0.5:
            sub, prio, ttl = "emerging", 0.6, 21
        elif info.get("state") == "dormant" and info.get("rediscovery", 0) >= 0.3:
            sub, prio, ttl = "dormant", 0.5, 30
        if sub is None:
            continue
        name = interest_name(profile, k, titles)
        out.append(
            QuestionSpec(
                kind="interest",
                domain=profile.domain,
                dedupe_key=f"interest:{sub}:{it.medoid_key}",
                priority=round(prio, 3),
                ttl_days=ttl,
                reask_days=120,
                payload={
                    "subkind": sub,
                    "prompt": INTEREST_PROMPTS[sub].format(name=name),
                    "answers": [{"response": r, "label": label} for r, label in INTEREST_ANSWERS[sub]],
                    "interest": {
                        "name": name,
                        "anchor_key": it.medoid_key,
                        "members": it.members[:8],
                        "examples": [titles.get(m, m) for m in it.members[:4]],
                    },
                },
            )
        )
    return out


def abandon_questions(profile: TasteProfile, titles: dict[str, str], items: dict[str, dict]) -> list[QuestionSpec]:
    return [
        QuestionSpec(
            kind="abandon",
            domain=profile.domain,
            dedupe_key=f"abandon:{k}",
            priority=0.7,
            ttl_days=30,
            reask_days=3650,
            payload={
                "prompt": f"You stopped {titles.get(k, 'this')} early. What happened?",
                "answers": [{"reason": r, "label": label} for r, label in ABANDON_REASONS],
                "items": [items[k]] if k in items else [{"canon_key": k, "title": titles.get(k)}],
            },
        )
        for k in profile.abandoned[:5]
    ]


def onboarding_question(domain: str, items: list[dict], cold: bool) -> QuestionSpec | None:
    if not items:
        return None
    return QuestionSpec(
        kind="onboarding",
        domain=domain,
        dedupe_key=f"onboarding:{domain}",
        priority=0.9 if cold else 0.45,
        ttl_days=14,
        reask_days=30,
        payload={
            "prompt": "Tap the titles you love. Mark any that aren't for you.",
            "answers": ["loved", "liked", "disliked", "unknown"],
            "items": items,
        },
    )


def pair_question(domain: str, a: dict, b: dict) -> QuestionSpec:
    ka, kb = sorted([a["canon_key"], b["canon_key"]])
    return QuestionSpec(
        kind="this_or_that",
        domain=domain,
        dedupe_key=f"pair:{ka}:{kb}",
        priority=0.4,
        ttl_days=7,
        reask_days=180,
        payload={"prompt": "Which would you rather watch?", "answers": ["a", "b", "both", "neither", "skip"], "a": a, "b": b},
    )


def tag_question_spec(domain: str, tag: str, value: float, examples: list[dict]) -> QuestionSpec:
    from .elicitation import TAG_PROMPTS, pretty_tag

    ns = tag.split(":", 1)[0]
    return QuestionSpec(
        kind="tag",
        domain=domain,
        dedupe_key=f"tag:{tag}",
        priority=round(0.35 + min(value, 1.0) * 0.3, 3),
        ttl_days=21,
        reask_days=365,
        payload={
            "tag": tag,
            "prompt": TAG_PROMPTS.get(ns, "How do you feel about {x}?").format(x=pretty_tag(tag)),
            "answers": [{"response": "love", "label": "Love it"}, {"response": "fine", "label": "It's fine"},
                        {"response": "not_for_me", "label": "Not for me"}],
            "examples": examples,  # a few titles with this tag, for context
        },
    )


# How to answer, shown with every queue entry so a client (or an LLM over MCP) needs no docs.
ANSWER_TEMPLATES = {
    "onboarding": {"kind": "onboarding", "loved": ["<canon_key>"], "liked": [], "disliked": []},
    "abandon": {"kind": "abandon", "canon_key": "<canon_key>", "reason": "<one of answers[].reason>"},
    "interest": {"kind": "interest", "response": "<one of answers[].response>"},
    "this_or_that": {"kind": "this_or_that", "a": "<a.canon_key>", "b": "<b.canon_key>", "answer": "a|b|both|neither|skip"},
    "tag": {"kind": "tag", "response": "love|fine|not_for_me"},
}
