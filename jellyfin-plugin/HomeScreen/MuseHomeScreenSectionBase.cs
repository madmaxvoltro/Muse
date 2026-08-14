using System;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Querying;
using Muse.JellyfinPlugin.Services;

namespace Muse.JellyfinPlugin.HomeScreen;

/// <summary>
/// Shared logic for every Muse homepage row: call the Recommendation API, resolve the
/// returned `source_item_id`s back to real Jellyfin BaseItems, return a QueryResult.
///
/// Resolution only works for source="jellyfin" items — which is all a homepage row can
/// meaningfully show anyway (you can't play something Jellyfin doesn't have). This relies
/// on the Jellyfin adapter (adapters/jellyfin/) having used Jellyfin's own item GUIDs as
/// `source_item_id` when it wrote those events — see docs/adapters/jellyfin.md.
///
/// See HomeScreen/README.md before relying on this in production — the IHomeScreenSection
/// interface this implements hasn't been compiled against a real Jellyfin.Controller build.
/// </summary>
public abstract class MuseHomeScreenSectionBase
{
    protected readonly ILibraryManager LibraryManager;
    protected readonly MuseApiClient ApiClient;

    protected MuseHomeScreenSectionBase(ILibraryManager libraryManager)
    {
        LibraryManager = libraryManager;
        var baseUrl = Plugin.Instance?.Configuration.RecommendationApiUrl ?? "http://recommendation-api:8001";
        ApiClient = new MuseApiClient(new HttpClient(), baseUrl);
    }

    protected int RowLimit => Plugin.Instance?.Configuration.RowItemLimit ?? 20;

    protected QueryResult<BaseItem> ResolveToBaseItems(string[] jellyfinItemIdsHex)
    {
        var items = jellyfinItemIdsHex
            .Select(idHex => Guid.TryParse(idHex, out var guid) ? LibraryManager.GetItemById(guid) : null)
            .Where(item => item is not null)
            .Cast<BaseItem>()
            .ToArray();

        return new QueryResult<BaseItem>
        {
            Items = items,
            TotalRecordCount = items.Length,
        };
    }
}
