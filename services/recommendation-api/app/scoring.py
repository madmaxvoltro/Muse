"""Stage 2 ranking + stage 3 re-ranking. Deliberately a hand-weighted, explainable
scoring function rather than a learned model (see docs/architecture.md — chosen for
explainability and because a learned model needs feedback volume v1 won't have).
"""

import math
import re
from dataclasses import dataclass, field

# Weights are intentionally named constants, not magic numbers, so tuning them
# is a one-line change and every score is traceable to a specific factor —
# that traceability IS the explainability feature.
WEIGHT_SIMILARITY = 0.5
WEIGHT_RECENCY_OF_SIMILAR = 0.15
WEIGHT_WITHIN_USER_POPULARITY = 0.15
WEIGHT_COMPLETION_RATE = 0.1
WEIGHT_CONFIDENCE = 0.1

WEIGHT_STATED_PREFERENCE = 0.2  # a fact the person/an assistant told Muse that names this item's subject

EXPLORE_RATIO_DEFAULT = 0.125  # 12.5%, midpoint of the 10-15% range decided in design
DIVERSITY_MAX_PER_GENRE = 3


@dataclass
class Candidate:
    source: str
    source_item_id: str
    item_type: str
    similarity: float  # cosine similarity to taste vector, stage 1 output
    recency_of_similar_days: float  # age in days of the most recent similar interaction
    within_user_popularity: float  # 0-1, normalized count of similar items interacted with
    completion_rate: float  # 0-1, how often similar content was completed vs abandoned
    confidence: float  # 0-1, see confidence()
    genres: list[str] = field(default_factory=list)
    title: str = ""

    score: float = 0.0
    reasons: dict[str, float] = field(default_factory=dict)


def confidence(interaction_count: int, variance: float, avg_age_days: float, cross_source_agreement: float) -> float:
    """0-1 confidence estimate. Low interaction count, high variance, old signal,
    or no cross-source agreement all pull confidence down.
    """
    count_term = min(interaction_count / 10.0, 1.0)
    variance_term = max(1.0 - variance, 0.0)
    age_term = math.exp(-avg_age_days / 90.0)
    return max(0.0, min(1.0, 0.4 * count_term + 0.2 * variance_term + 0.2 * age_term + 0.2 * cross_source_agreement))


def score_candidate(c: Candidate) -> Candidate:
    recency_term = math.exp(-c.recency_of_similar_days / 30.0)

    reasons = {
        "similarity": WEIGHT_SIMILARITY * c.similarity,
        "recency_of_similar": WEIGHT_RECENCY_OF_SIMILAR * recency_term,
        "within_user_popularity": WEIGHT_WITHIN_USER_POPULARITY * c.within_user_popularity,
        "completion_rate": WEIGHT_COMPLETION_RATE * c.completion_rate,
        "confidence": WEIGHT_CONFIDENCE * c.confidence,
    }
    c.reasons = reasons
    c.score = sum(reasons.values())
    return c


def rerank(
    candidates: list[Candidate],
    limit: int,
    explore_ratio: float = EXPLORE_RATIO_DEFAULT,
    diversity_max_per_genre: int = DIVERSITY_MAX_PER_GENRE,
) -> list[Candidate]:
    """Stage 3: diversity cap + explore-slot allocation.

    Explore slots are deliberately filled from *low-confidence, still-decent-score*
    candidates rather than dropped — the multi-armed-bandit pattern from
    docs/architecture.md: showing them is how confidence improves next cycle.
    """
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)

    explore_slots = max(1, round(limit * explore_ratio)) if candidates else 0
    exploit_slots = limit - explore_slots

    exploit_pool = [c for c in ranked if c.confidence >= 0.5]
    explore_pool = sorted(
        [c for c in ranked if c.confidence < 0.5],
        key=lambda c: c.score,
        reverse=True,
    )

    selected: list[Candidate] = []
    genre_counts: dict[str, int] = {}

    def fits_diversity(c: Candidate) -> bool:
        return all(genre_counts.get(g, 0) < diversity_max_per_genre for g in c.genres)

    def take(pool: list[Candidate], n: int) -> None:
        added = 0
        for c in pool:
            if added >= n:
                break
            if c in selected or not fits_diversity(c):
                continue
            selected.append(c)
            for g in c.genres:
                genre_counts[g] = genre_counts.get(g, 0) + 1
            added += 1

    take(exploit_pool, exploit_slots)
    take(explore_pool, explore_slots)

    # Backfill from whatever's left if diversity constraints starved a bucket.
    if len(selected) < limit:
        remaining = [c for c in ranked if c not in selected]
        take(remaining, limit - len(selected))

    return selected[:limit]


def _mentions(subject: str, c: Candidate) -> bool:
    """Whole-word, case-insensitive match of a fact's subject in the title or a genre."""
    subj = subject.strip().casefold()
    if len(subj) < 2:
        return False
    pattern = re.compile(r"(?<!\w)" + re.escape(subj) + r"(?!\w)")
    return bool(pattern.search(c.title.casefold())) or any(pattern.search(g.casefold()) for g in c.genres)


def apply_knowledge(
    candidates: list[Candidate], facts: list[dict], source_weights: dict[str, float] | None = None
) -> list[Candidate]:
    """Applies what Muse was told on top of the behavioural score (after score_candidate):

    - 'avoid' facts with a subject are a hard filter ("never suggest horror");
    - 'likes' / 'dislikes' facts with a subject add or subtract WEIGHT_STATED_PREFERENCE,
      scaled by sentiment and confidence, and show up as the `stated_preference` reason;
    - per-source overlay weights (user_overlay.source_weights) scale the final score.
    The facts' statements also shape the taste vector itself (embedding worker); this is the
    precise, explainable part for facts that name something.
    """
    source_weights = source_weights or {}
    anchored = [f for f in facts if f.get("subject")]
    out: list[Candidate] = []
    for c in candidates:
        if any(f["kind"] == "avoid" and _mentions(f["subject"], c) for f in anchored):
            continue
        boost = 0.0
        for f in anchored:
            if f["kind"] in ("likes", "dislikes") and _mentions(f["subject"], c):
                boost += WEIGHT_STATED_PREFERENCE * float(f["sentiment"]) * float(f["confidence"])
        if boost:
            c.reasons["stated_preference"] = boost
            c.score += boost
        w = source_weights.get(c.source)
        if w is not None and w != 1.0:
            c.reasons["source_weight"] = c.score * (float(w) - 1.0)
            c.score *= float(w)
        out.append(c)
    return out


def describe(c: Candidate, facts: list[dict] | None = None) -> list[str]:
    """Human-readable reasons, strongest first — for UIs and assistants. The numeric
    `explanation` stays the source of truth."""
    why: list[tuple[float, str]] = []
    if c.similarity >= 0.55:
        why.append((c.reasons.get("similarity", 0), "Very close to what you enjoy"))
    elif c.similarity >= 0.4:
        why.append((c.reasons.get("similarity", 0), "Similar to what you enjoy"))
    if c.reasons.get("stated_preference", 0) > 0:
        subjects = [f["subject"] for f in facts or [] if f.get("subject") and f["kind"] == "likes" and _mentions(f["subject"], c)]
        why.append((c.reasons["stated_preference"] + 1, f"You said you like {subjects[0]}" if subjects else "Matches something you told Muse"))
    if c.completion_rate >= 0.6 and c.genres:
        why.append((c.reasons.get("completion_rate", 0), f"You usually finish {c.genres[0]}"))
    if c.recency_of_similar_days <= 7 and c.genres:
        why.append((c.reasons.get("recency_of_similar", 0), f"You've been into {c.genres[0]} lately"))
    if c.confidence < 0.5:
        why.append((-1, "Something new to try"))
    return [text for _, text in sorted(why, key=lambda x: -x[0])]
