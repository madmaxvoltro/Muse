"""What Muse "knows" about an item: one text document for the embedding model, and a set of
structured tags for exact matching.

Two complementary representations, because each fails where the other is strong:

* The **dense embedding** of a rich document captures plot, tone and theme ("melancholic
  space survival") and generalises to titles that share no metadata. It is fuzzy about names.
* **Sparse tags** (keywords, director, cast, writer, studio, collection, language, decade)
  are exact. "Another Denis Villeneuve film" or "more A24" is precise in tag space and
  blurry in embedding space.

The old item text was title + overview + genres. TMDB keywords alone ("time loop",
"unreliable narrator", "found family") are among the most informative content features for
films, so enrichment adds them, plus credits and optional LLM tone descriptors.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .canon import norm_text

TYPE_LABEL = {
    "movie": "Film",
    "series": "TV series",
    "series_episode": "TV episode",
    "youtube_video": "YouTube video",
    "track": "Song",
    "audiobook": "Audiobook",
    "ebook": "Book",
}

MAX_KEYWORDS = 25
MAX_CAST = 5


def _list(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    out = []
    for x in v:
        if isinstance(x, dict):
            x = x.get("name") or x.get("Name")
        if x:
            out.append(str(x))
    return out


def merged_fields(item_type: str, metadata: dict, enrichment: dict | None = None, llm: dict | None = None) -> dict:
    """One flat view over adapter metadata, TMDB enrichment and LLM descriptors. Enrichment
    wins for curated fields (keywords, credits); adapter metadata wins for what the library
    itself says (title as shown in Jellyfin)."""
    md = metadata or {}
    en = enrichment or {}
    people = md.get("people") or []
    directors = _list(en.get("directors")) or [p.get("name") for p in people if isinstance(p, dict) and p.get("type") == "Director" and p.get("name")]
    writers = _list(en.get("writers")) or [p.get("name") for p in people if isinstance(p, dict) and p.get("type") == "Writer" and p.get("name")]
    cast = _list(en.get("cast")) or [p.get("name") for p in people if isinstance(p, dict) and p.get("type") == "Actor" and p.get("name")]
    year = md.get("year") or en.get("year")
    return {
        "title": md.get("title") or en.get("title") or "",
        "original_title": en.get("original_title") if en.get("original_title") != (md.get("title") or en.get("title")) else None,
        "type": item_type,
        "year": int(year) if isinstance(year, (int, float)) or (isinstance(year, str) and year.isdigit()) else None,
        "genres": list(dict.fromkeys(_list(md.get("genres")) + _list(en.get("genres")))),
        "keywords": _list(en.get("keywords"))[:MAX_KEYWORDS] + _list(md.get("tags"))[:10],
        "directors": directors[:3],
        "writers": writers[:3],
        "cast": cast[:MAX_CAST],
        "studios": (_list(en.get("companies")) or _list(md.get("studios")))[:3],
        "networks": _list(en.get("networks"))[:2],
        "language": en.get("original_language") or md.get("language"),
        "countries": _list(en.get("countries"))[:3],
        "collection": en.get("collection"),  # {"id":..., "name":...}
        "tagline": en.get("tagline") or (md.get("taglines") or [None])[0],
        "overview": en.get("overview") if len(en.get("overview") or "") > len(md.get("description") or "") else (md.get("description") or ""),
        "certification": md.get("official_rating") or en.get("certification"),
        "runtime_min": en.get("runtime") or md.get("runtime_min"),
        "artist": md.get("artist") or md.get("album_artist"),
        "album": md.get("album"),
        "channel": md.get("channel"),
        "author": md.get("author"),
        "narrator": md.get("narrator"),
        "query": md.get("query"),
        "tone": (llm or {}).get("tone"),
        "themes": (llm or {}).get("themes"),
        "audience": (llm or {}).get("audience"),
    }


def build_document(f: dict) -> str:
    """Natural-language document for the embedder. Ordered most-discriminative first:
    embedding models weight early tokens more and truncate the tail."""
    parts: list[str] = []
    head = f["title"]
    if f.get("year"):
        head += f" ({f['year']})"
    label = TYPE_LABEL.get(f["type"], f["type"])
    parts.append(f"{head}. {label}.")
    if f.get("original_title"):
        parts.append(f"Original title: {f['original_title']}.")
    if f.get("artist"):
        parts.append(f"By {f['artist']}" + (f", from the album {f['album']}." if f.get("album") else "."))
    if f.get("author"):
        parts.append(f"Written by {f['author']}.")
    if f.get("channel"):
        parts.append(f"Channel: {f['channel']}.")
    if f.get("genres"):
        parts.append("Genres: " + ", ".join(f["genres"]) + ".")
    if f.get("tone"):
        parts.append("Tone: " + (", ".join(f["tone"]) if isinstance(f["tone"], list) else str(f["tone"])) + ".")
    if f.get("themes"):
        parts.append("Themes: " + (", ".join(f["themes"]) if isinstance(f["themes"], list) else str(f["themes"])) + ".")
    if f.get("keywords"):
        parts.append("Keywords: " + ", ".join(f["keywords"]) + ".")
    if f.get("tagline"):
        parts.append(f"{f['tagline']}")
    if f.get("overview"):
        parts.append(f["overview"].strip())
    if f.get("directors"):
        parts.append("Directed by " + ", ".join(f["directors"]) + ".")
    if f.get("cast"):
        parts.append("Starring " + ", ".join(f["cast"][:3]) + ".")
    if f.get("collection") and isinstance(f["collection"], dict) and f["collection"].get("name"):
        parts.append(f"Part of {f['collection']['name']}.")
    if f.get("query"):
        parts.append(f"Search: {f['query']}")
    return " ".join(p for p in parts if p).strip()


def build_tags(f: dict) -> list[str]:
    """Namespaced exact-match tags. Namespaces keep 'Drama' the genre apart from a film
    titled 'Drama' and let the ranker weight people differently from keywords."""
    tags: list[str] = []

    def add(prefix: str, values: list[str] | None) -> None:
        for v in values or []:
            n = norm_text(v)
            if n:
                tags.append(f"{prefix}:{n}")

    add("g", f.get("genres"))
    add("k", f.get("keywords"))
    add("d", f.get("directors"))
    add("w", f.get("writers"))
    add("p", f.get("cast"))
    add("s", f.get("studios"))
    add("n", f.get("networks"))
    add("a", [f["artist"]] if f.get("artist") else None)
    add("au", [f["author"]] if f.get("author") else None)
    add("ch", [f["channel"]] if f.get("channel") else None)
    add("t", f.get("tone") if isinstance(f.get("tone"), list) else None)
    add("th", f.get("themes") if isinstance(f.get("themes"), list) else None)
    if f.get("language"):
        tags.append(f"lang:{str(f['language']).lower()}")
    if f.get("year"):
        tags.append(f"decade:{(f['year'] // 10) * 10}s")
    coll = f.get("collection")
    if isinstance(coll, dict) and coll.get("id"):
        tags.append(f"c:{coll['id']}")
    return list(dict.fromkeys(tags))


def document_hash(doc: str, model_name: str) -> str:
    """Re-embed only when the text or the model changed."""
    return hashlib.sha256(f"{model_name}\x00{doc}".encode()).hexdigest()[:32]


# TMDB tags weigh differently in the ranker: a shared director says far more than a shared decade.
TAG_NAMESPACE_WEIGHT = {
    "d": 1.6,
    "w": 1.2,
    "c": 1.5,
    "k": 1.0,
    "th": 1.0,
    "t": 0.9,
    "p": 0.8,
    "a": 1.6,
    "au": 1.6,
    "ch": 1.4,
    "s": 0.6,
    "n": 0.5,
    "g": 0.5,
    "lang": 0.6,
    "decade": 0.3,
}


def tag_namespace(tag: str) -> str:
    return tag.split(":", 1)[0]
