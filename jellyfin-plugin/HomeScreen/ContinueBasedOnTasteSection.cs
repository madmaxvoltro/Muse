using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Querying;

namespace Muse.JellyfinPlugin.HomeScreen;

/// <summary>
/// "Continue based on taste" — partially-watched items re-ranked by taste-match, per
/// docs/architecture.md. NOT YET BACKED BY AN API ENDPOINT: the Recommendation API's
/// /recommendations endpoint deliberately excludes already-owned/interacted-with items
/// (see candidates.py get_owned_item_ids) — that's correct for "Recommended for You" but
/// wrong for this row, which specifically wants in-progress items. Needs a new endpoint
/// (e.g. GET /continue-watching/{user_id}) querying the event table for partially-watched
/// items (progress_pct between ~10-90, same range used as the curator's protection
/// threshold) ranked by taste score, before this section does anything useful.
/// See HomeScreen/README.md — interface shape also not yet verified against a real build.
/// </summary>
public class ContinueBasedOnTasteSection : MuseHomeScreenSectionBase
{
    public ContinueBasedOnTasteSection(ILibraryManager libraryManager) : base(libraryManager)
    {
    }

    public string Section => "MuseContinueBasedOnTaste";

    public string DisplayText => "Continue Based on Taste";

    public QueryResult<BaseItem> GetResults()
    {
        // TODO: call a not-yet-implemented Recommendation API endpoint once it exists.
        return new QueryResult<BaseItem> { Items = System.Array.Empty<BaseItem>(), TotalRecordCount = 0 };
    }
}
