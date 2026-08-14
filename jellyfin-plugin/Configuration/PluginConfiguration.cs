using MediaBrowser.Model.Plugins;

namespace Muse.JellyfinPlugin.Configuration;

/// <summary>
/// Everything the plugin needs to reach Muse. No taste-profile data is stored here —
/// this is connection config only, the actual profile lives in Muse's own Postgres/Qdrant.
/// </summary>
public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>
    /// Base URL of the Muse Recommendation API, e.g. http://muse-recommendation-api:8001.
    /// Reachable from the Jellyfin container/host over the same internal network Muse's
    /// docker-compose sets up — see docs/installation.md.
    /// </summary>
    public string RecommendationApiUrl { get; set; } = "http://recommendation-api:8001";

    /// <summary>
    /// How many items to request per homepage row.
    /// </summary>
    public int RowItemLimit { get; set; } = 20;
}
