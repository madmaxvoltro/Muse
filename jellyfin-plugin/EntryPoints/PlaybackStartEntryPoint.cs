using System;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Controller.Session;
using Microsoft.Extensions.Logging;
using Muse.JellyfinPlugin.Services;

namespace Muse.JellyfinPlugin.EntryPoints;

/// <summary>
/// The real-time half of the "Soon Gone" mechanism (see docs/architecture.md — the polling
/// arr/Jellyfin adapters can't do this, they only see state changes on their next poll
/// cycle, which could be minutes after the 48h grace window already expired).
///
/// Subscribes to ISessionManager.PlaybackStart — the moment a user hits play on ANYTHING,
/// checks whether it's currently in Muse's Soon Gone list, and if so, calls the whitelist
/// endpoint immediately. Playback-start alone counts; no minimum watch duration.
/// </summary>
public class PlaybackStartEntryPoint : IServerEntryPoint
{
    private readonly ISessionManager _sessionManager;
    private readonly ILogger<PlaybackStartEntryPoint> _logger;
    private readonly MuseApiClient _apiClient;

    public PlaybackStartEntryPoint(ISessionManager sessionManager, ILogger<PlaybackStartEntryPoint> logger)
    {
        _sessionManager = sessionManager;
        _logger = logger;
        var baseUrl = Plugin.Instance?.Configuration.RecommendationApiUrl ?? "http://recommendation-api:8001";
        _apiClient = new MuseApiClient(new HttpClient(), baseUrl);
    }

    public Task RunAsync()
    {
        _sessionManager.PlaybackStart += OnPlaybackStart;
        return Task.CompletedTask;
    }

    private async void OnPlaybackStart(object? sender, PlaybackProgressEventArgs e)
    {
        if (e.Item is null || e.Users.Count == 0)
        {
            return;
        }

        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var soonGone = await _apiClient.GetSoonGoneAsync(cts.Token).ConfigureAwait(false);

            var itemId = e.Item.Id.ToString("N");
            var isPending = Array.Exists(soonGone, sg => sg.SourceItemId == itemId);
            if (!isPending)
            {
                return;
            }

            foreach (var user in e.Users)
            {
                var whitelisted = await _apiClient.WhitelistAsync(itemId, user.Id, cts.Token).ConfigureAwait(false);
                if (whitelisted)
                {
                    _logger.LogInformation("Muse: whitelisted Soon Gone item {ItemId} (played by {UserId})", itemId, user.Id);
                }
            }
        }
        catch (Exception ex)
        {
            // Never let a Muse API hiccup break playback for the user.
            _logger.LogWarning(ex, "Muse: failed to check/whitelist Soon Gone status for item {ItemId}", e.Item?.Id);
        }
    }

    public void Dispose()
    {
        _sessionManager.PlaybackStart -= OnPlaybackStart;
        GC.SuppressFinalize(this);
    }
}
