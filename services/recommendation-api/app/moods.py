"""Mood presets ("Tonight I want…"). A mood is a temporary overlay: it steers the picks
rows for a few hours and never touches the behaviour-derived taste vector.

Each mood is a prompt embedded with the content model, blended into the query vector, plus
optional hard constraints. Prompts are written in the vocabulary of overviews and genres,
because that is what the content vectors were built from.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mood:
    id: str
    prompt: str | None  # blended into the query vector (None: constraints only)
    weight: float = 0.4  # share of the query vector the prompt takes
    max_runtime_minutes: int | None = None
    rewatch: bool = False  # candidates come from the profile's own loved history


MOODS: dict[str, Mood] = {
    m.id: m
    for m in (
        Mood("light", "light-hearted funny feel-good comedy, warm and charming, laughs, romance, family fun"),
        Mood("edge", "tense gripping thriller, suspense, edge of your seat, danger, chase, crime, high stakes action"),
        Mood("short", None, max_runtime_minutes=100),
        Mood("mindbending", "mind-bending cerebral science fiction, twist ending, puzzle, time travel, reality, philosophical mystery"),
        Mood("comfort", None, rewatch=True),
        Mood("cozy", "cozy gentle heartwarming story, small town, friendship, slow and soothing", weight=0.35),
        Mood("epic", "epic sweeping adventure, grand battles, fantasy worlds, heroes, spectacle", weight=0.35),
    )
}

DEFAULT_MOOD_HOURS = 4.0


def get(mood_id: str | None) -> Mood | None:
    return MOODS.get(mood_id) if mood_id else None
