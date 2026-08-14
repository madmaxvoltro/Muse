using System;
using System.Collections.Generic;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using Muse.JellyfinPlugin.Configuration;

namespace Muse.JellyfinPlugin;

/// <summary>
/// Plugin bootstrap. Deliberately scoped to the official IHomeScreenSection API
/// (Jellyfin 10.9+) rather than forking jellyfin-web — see docs/architecture.md
/// "Jellyfin plugin scope" for why: no client fork to maintain across Jellyfin updates.
/// This plugin only ever adds rows to the existing homepage, never restructures it.
/// </summary>
public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    public static Plugin? Instance { get; private set; }

    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;
    }

    public override string Name => "Muse";

    public override Guid Id => Guid.Parse("b7e3a6b0-5b8a-4b2a-9e3d-6f1b1b4a2f2a");

    public override string Description =>
        "Adds Muse's cross-domain recommendations (Recommended for You, Continue based on " +
        "taste, Soon Gone) to the Jellyfin homepage.";

    public IEnumerable<PluginPageInfo> GetPages()
    {
        yield return new PluginPageInfo
        {
            Name = "Muse",
            EmbeddedResourcePath = string.Format("{0}.Configuration.configPage.html", GetType().Namespace),
        };
    }
}
