using System;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

namespace Muse.JellyfinPlugin.Services;

public class MuseRecommendationItem
{
    [JsonPropertyName("source")]
    public string Source { get; set; } = string.Empty;

    [JsonPropertyName("source_item_id")]
    public string SourceItemId { get; set; } = string.Empty;

    [JsonPropertyName("score")]
    public double Score { get; set; }

    [JsonPropertyName("confidence")]
    public double Confidence { get; set; }

    [JsonPropertyName("explanation")]
    public System.Collections.Generic.Dictionary<string, double>? Explanation { get; set; }
}

public class MuseRecommendationResponse
{
    [JsonPropertyName("recommendations")]
    public MuseRecommendationItem[] Recommendations { get; set; } = Array.Empty<MuseRecommendationItem>();
}

public class MuseSoonGoneItem
{
    [JsonPropertyName("source_item_id")]
    public string SourceItemId { get; set; } = string.Empty;

    [JsonPropertyName("item_type")]
    public string ItemType { get; set; } = string.Empty;

    [JsonPropertyName("expires_at")]
    public DateTimeOffset ExpiresAt { get; set; }
}

public class MuseSoonGoneListResponse
{
    [JsonPropertyName("items")]
    public MuseSoonGoneItem[] Items { get; set; } = Array.Empty<MuseSoonGoneItem>();
}

/// <summary>
/// Thin HTTP client for the Muse Recommendation API. All three homepage sections and the
/// playback-start whitelist hook go through this — one place to change if the API shape
/// changes, and one place that owns the base-URL/timeout config.
/// </summary>
public class MuseApiClient
{
    private readonly HttpClient _http;

    public MuseApiClient(HttpClient httpClient, string baseUrl)
    {
        _http = httpClient;
        _http.BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/");
        _http.Timeout = TimeSpan.FromSeconds(5);
    }

    public async Task<MuseRecommendationItem[]> GetRecommendationsAsync(
        Guid userId, string itemType, int limit, CancellationToken cancellationToken)
    {
        var url = $"recommendations/{userId}?item_type={itemType}&limit={limit}";
        var response = await _http.GetFromJsonAsync<MuseRecommendationResponse>(url, cancellationToken)
            .ConfigureAwait(false);
        return response?.Recommendations ?? Array.Empty<MuseRecommendationItem>();
    }

    public async Task<MuseSoonGoneItem[]> GetSoonGoneAsync(CancellationToken cancellationToken)
    {
        var response = await _http.GetFromJsonAsync<MuseSoonGoneListResponse>("soon-gone", cancellationToken)
            .ConfigureAwait(false);
        return response?.Items ?? Array.Empty<MuseSoonGoneItem>();
    }

    /// <summary>
    /// Called from the playback-start hook when a user opens a "Soon Gone" item.
    /// Playback-start alone is enough to trigger this — no minimum watch duration,
    /// a deliberate simplicity tradeoff (see docs/architecture.md).
    /// </summary>
    public async Task<bool> WhitelistAsync(string sourceItemId, Guid userId, CancellationToken cancellationToken)
    {
        var response = await _http.PostAsync(
            $"soon-gone/{sourceItemId}/whitelist?user_id={userId}",
            content: null,
            cancellationToken).ConfigureAwait(false);
        return response.IsSuccessStatusCode;
    }
}
