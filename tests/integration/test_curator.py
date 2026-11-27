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


def test_soon_gone_saves_what_people_want_and_removes_the_rest(db_dsn):
    """Watched recently, on someone's list or pinned: stays. Otherwise: removed through the arr API."""
    import httpx
    import main as curator
    from arr_client import ArrConfig
    from budget import protection
    from fixtures import SERIES_TMDB, insert
    from muse_schema.crypto import P_ADMIN_NOTE, get_vault

    deleted = []

    def arr(request):
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(200)
        return httpx.Response(404)

    cfg = ArrConfig(radarr_url="http://radarr", radarr_api_key="k", sonarr_url="http://sonarr", sonarr_api_key="k")

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        async with pool.acquire() as conn:
            for tmdb_id in (107, 108, 109, 110):
                await conn.execute(
                    "INSERT INTO curator_managed_item (source, source_item_id, item_type, estimated_bytes, arr_id) VALUES ('arr', $1, 'movie', 1, $2)",
                    str(tmdb_id), f"a{tmdb_id}",
                )
                await conn.execute(
                    "INSERT INTO soon_gone (source_item_id, item_type, arr_id, expires_at) VALUES ($1, 'movie', $2, now() - interval '1 hour')",
                    str(tmdb_id), f"a{tmdb_id}",
                )
            await conn.execute("UPDATE curator_managed_item SET pinned = TRUE WHERE source_item_id = '109'")
            # B put 107 on a list (in Vesper, via Jellyfin's ids); B watched 110 two days ago
            await insert(conn, USER_B, "jellyfin", "jf107", "movie", "added_watchlist", 3, 0.4)
            await insert(conn, USER_B, "jellyfin", "jf110", "movie", "played", 2, 0.3, 30)
        series_reason = await protection(pool, "series_episode", str(SERIES_TMDB))  # A binged it within 30 days
        async with httpx.AsyncClient(transport=httpx.MockTransport(arr)) as client:
            result = await curator.run_soon_gone_checks(pool, client, cfg)
        async with pool.acquire() as conn:
            gone = {r["source_item_id"] for r in await conn.fetch("SELECT source_item_id FROM curator_managed_item WHERE removed_at IS NOT NULL")}
            notes = [(r["kind"], get_vault().open_json(r["data"], P_ADMIN_NOTE)) for r in await conn.fetch("SELECT kind, data FROM admin_notification ORDER BY created_at")]
            kinds = [r["kind"] for r in await conn.fetch("SELECT kind FROM curator_activity")]
        await pool.close()
        return series_reason, result, gone, notes, kinds

    series_reason, result, gone, notes, kinds = asyncio.run(go())
    assert series_reason == "watched_recently"
    assert sorted(result["saved"]) == ["107", "109", "110"]
    assert result["removed"] == ["108"] and deleted == ["/api/v3/movie/a108"]
    assert gone == {"108"}
    assert sorted(k for k, _ in notes) == ["curator.removed", "curator.saved", "curator.saved", "curator.saved"]
    saved_text = " ".join(n["body"] for k, n in notes if k == "curator.saved")
    assert "on someone's list" in saved_text and "watched it recently" in saved_text and "kept by the admin" in saved_text
    assert sorted(kinds) == ["removed", "saved", "saved", "saved"]
