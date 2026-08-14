using System.Linq;
using System.Threading;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Querying;

namespace Muse.JellyfinPlugin.HomeScreen;

/// <summary>
/// "Recommended for You" — the top-scored candidates from the Recommendation API's
/// full 3-stage funnel (candidate generation -> ranking -> diversity/explore re-ranking).
/// See HomeScreen/README.md — interface shape not yet verified against a real build.
/// </summary>
public class RecommendedForYouSection : MuseHomeScreenSectionBase
{
    public RecommendedForYouSection(ILibraryManager libraryManager) : base(libraryManager)
    {
    }

    public string Section => "MuseRecommendedForYou";

    public string DisplayText => "Recommended for You";

    public QueryResult<BaseItem> GetResults(System.Guid userId, string itemType)
    {
        var recommendations = ApiClient
            .GetRecommendationsAsync(userId, itemType, RowLimit, CancellationToken.None)
            .GetAwaiter()
            .GetResult();

        var jellyfinItemIds = recommendations
            .Where(r => r.Source == "jellyfin")
            .Select(r => r.SourceItemId)
            .ToArray();

        return ResolveToBaseItems(jellyfinItemIds);
    }
}
