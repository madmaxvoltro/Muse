using System.Linq;
using System.Threading;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Querying;

namespace Muse.JellyfinPlugin.HomeScreen;

/// <summary>
/// "Soon Gone" — items the curator has marked for removal, currently in their 48h grace
/// window (see docs/architecture.md). Opening one of these from here is exactly what
/// triggers the whitelist-by-watching hook in EntryPoints/PlaybackStartEntryPoint.cs.
/// See HomeScreen/README.md — interface shape not yet verified against a real build.
/// </summary>
public class SoonGoneSection : MuseHomeScreenSectionBase
{
    public SoonGoneSection(ILibraryManager libraryManager) : base(libraryManager)
    {
    }

    public string Section => "MuseSoonGone";

    public string DisplayText => "Soon Gone";

    public QueryResult<BaseItem> GetResults()
    {
        var soonGone = ApiClient.GetSoonGoneAsync(CancellationToken.None).GetAwaiter().GetResult();

        var jellyfinItemIds = soonGone
            .OrderBy(item => item.ExpiresAt)
            .Take(RowLimit)
            .Select(item => item.SourceItemId)
            .ToArray();

        return ResolveToBaseItems(jellyfinItemIds);
    }
}
