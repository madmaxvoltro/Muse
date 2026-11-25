import asyncio
import sys

from conftest import ROOT, FakeEmbedder
from fixtures import EXTERNAL_REC, USER_A, USER_B
from test_worker import run_worker
from muse_taste.store import blind_canon

sys.path.insert(0, str(ROOT / "services" / "curator"))


def test_curator_adds_what_the_household_would_like(db_dsn, monkeypatch):
    import main as curator
    import scoring

    added_calls = []

    async def fake_add(client, config, item_type, lookup_id):
        added_calls.append((item_type, lookup_id))
        return f"arr-{lookup_id}"

    monkeypatch.setattr(curator, "add_wanted", fake_add)
    monkeypatch.setattr(curator, "ADD_SCORE_THRESHOLD", 0.2)

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        scorer = await scoring.ProfileScorer.load(pool, qdrant, [str(USER_A), str(USER_B)])
        from discovery import catalog_candidates

        cands = [c for c in catalog_candidates(qdrant, "movie") if c.key not in scorer.seen_by_anyone()]
        per = scorer.score(cands)
        ranked = sorted(((scoring.add_score(per[c.key]), c.key) for c in cands), reverse=True)
        added = await curator.run_add_cycle(pool, qdrant, FakeEmbedder(), None, None, None, [str(USER_A), str(USER_B)])
        async with pool.acquire() as conn:
            managed = await conn.fetch("SELECT source_item_id, arr_id FROM curator_managed_item")
        await pool.close()
        return ranked, added, managed

    ranked, added, managed = asyncio.run(go())
    assert ranked[0][1] == blind_canon(f"tmdb:movie:{EXTERNAL_REC}"), ranked[:3]
    assert added and added[0]["canon_key"] == blind_canon(f"tmdb:movie:{EXTERNAL_REC}")
    assert added_calls == [("movie", str(EXTERNAL_REC))]
    assert [(r["source_item_id"], r["arr_id"]) for r in managed] == [(str(EXTERNAL_REC), f"arr-{EXTERNAL_REC}")]
