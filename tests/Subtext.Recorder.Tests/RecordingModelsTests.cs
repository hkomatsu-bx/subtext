using Subtext.Capture;
using Subtext.Recorder;
using Xunit;

namespace Subtext.Recorder.Tests;

public sealed class RecordingModelsTests
{
    private static SidecarMeta SampleMeta(StreamRole role) => new(
        WavPath: role == StreamRole.Self ? "self.wav" : "others.wav",
        StreamRole: role,
        StartTimeUtc: new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc),
        SampleRate: 16000,
        Channels: 1,
        BitDepth: 16,
        DeviceName: "Test Device",
        DurationSec: 12.5,
        SilenceFilledSec: 1.0);

    [Fact]
    public void Manifest_SerializesEnumsAsCamelCaseStrings()
    {
        var manifest = new RecordingManifest(
            SessionId: "20260101-000000",
            CreatedAtUtc: new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc),
            Streams: new[] { SampleMeta(StreamRole.Self), SampleMeta(StreamRole.Others) },
            Status: RecordingStatus.Complete,
            CommonStartUtc: new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        string json = RecordingJson.Serialize(manifest);

        Assert.Contains("\"self\"", json);
        Assert.Contains("\"others\"", json);
        Assert.Contains("\"complete\"", json);
        Assert.Contains("\"sessionId\"", json); // camelCase プロパティ
    }

    [Fact]
    public void Manifest_RoundTripsThroughJson()
    {
        var original = new RecordingManifest(
            SessionId: "20260101-000000",
            CreatedAtUtc: new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc),
            Streams: new[] { SampleMeta(StreamRole.Self), SampleMeta(StreamRole.Others) },
            Status: RecordingStatus.Incomplete,
            CommonStartUtc: new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        string json = RecordingJson.Serialize(original);
        RecordingManifest restored = RecordingJson.Deserialize<RecordingManifest>(json);

        Assert.Equal(original.SessionId, restored.SessionId);
        Assert.Equal(RecordingStatus.Incomplete, restored.Status);
        Assert.Equal(2, restored.Streams.Count);
        Assert.Contains(restored.Streams, s => s.StreamRole == StreamRole.Self);
        Assert.Contains(restored.Streams, s => s.StreamRole == StreamRole.Others);
    }

    [Fact]
    public void SidecarMeta_RoundTripsThroughJson()
    {
        SidecarMeta original = SampleMeta(StreamRole.Others);

        string json = RecordingJson.Serialize(original);
        SidecarMeta restored = RecordingJson.Deserialize<SidecarMeta>(json);

        Assert.Equal(original, restored);
    }
}
