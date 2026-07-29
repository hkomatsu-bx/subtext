using System.Text.Json;
using System.Text.Json.Serialization;
using Subtext.Capture;

namespace Subtext.Recorder;

/// <summary>録音セッションの状態。Complete=正常終了、Incomplete=異常時の部分保存（Q7=A）。</summary>
public enum RecordingStatus
{
    Complete,
    Incomplete
}

/// <summary>
/// 各 WAV に付随するメタ（連携契約, FR-16）。Unit B が読む。
/// </summary>
public sealed record SidecarMeta(
    string WavPath,
    StreamRole StreamRole,
    DateTime StartTimeUtc,
    int SampleRate,
    int Channels,
    int BitDepth,
    string DeviceName,
    double DurationSec,
    double SilenceFilledSec);

/// <summary>
/// 録音セッションを束ねるマニフェスト（連携契約, FR-16）。Unit B / PostMeetingPipeline の入口。
/// streams は self と others のちょうど2件（BR-IO-03）。
/// </summary>
public sealed record RecordingManifest(
    string SessionId,
    DateTime CreatedAtUtc,
    IReadOnlyList<SidecarMeta> Streams,
    RecordingStatus Status,
    DateTime CommonStartUtc);

/// <summary>録音セッションの駆動パラメータ（値オブジェクト）。</summary>
public sealed record RecorderOptions(
    string OutputDir,
    string SessionId,
    TimeSpan MaxDuration,
    AudioDevice SelfDevice,
    AudioDevice OthersDevice);

/// <summary>
/// 連携契約 JSON のシリアライズ設定。camelCase・enum は文字列（self/others, complete/incomplete）。
/// </summary>
public static class RecordingJson
{
    public static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.CamelCase) }
    };

    public static string Serialize<T>(T value) => JsonSerializer.Serialize(value, Options);

    public static T Deserialize<T>(string json) =>
        JsonSerializer.Deserialize<T>(json, Options)
        ?? throw new InvalidOperationException($"Failed to deserialize {typeof(T).Name}.");
}
