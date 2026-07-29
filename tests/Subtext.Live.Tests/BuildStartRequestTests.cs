using Amazon.TranscribeStreaming;
using Amazon.TranscribeStreaming.Model;
using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

/// <summary>
/// <see cref="TranscribeStreamingClient.BuildStartRequest"/> の純ロジック検証（C1・FR-C1-01）。
/// AWS / WASAPI には触れず、request オブジェクトの組み立てのみを固定する（DG-C1-3・NFR-C1-04）。
/// </summary>
public sealed class BuildStartRequestTests
{
    private static Task<IAudioStreamEvent> DummyPublisher() => Task.FromResult<IAudioStreamEvent>(null!);

    private static StreamingOptions Options() =>
        StreamingOptions.ForSource(StreamSource.Others, "ja-JP", 16000);

    [Fact]
    public void BuildStartRequest_SetsCoreParameters()
    {
        StartStreamTranscriptionRequest request =
            TranscribeStreamingClient.BuildStartRequest(Options(), null, DummyPublisher);

        Assert.Equal("ja-JP", request.LanguageCode);
        Assert.Equal(16000, request.MediaSampleRateHertz);
        Assert.Equal(MediaEncoding.Pcm, request.MediaEncoding);
        Assert.False(request.ShowSpeakerLabel);
        Assert.NotNull(request.AudioStreamPublisher);
    }

    [Fact]
    public void BuildStartRequest_WithVocabularyName_SetsIt()
    {
        StartStreamTranscriptionRequest request =
            TranscribeStreamingClient.BuildStartRequest(Options(), "subtext-ja", DummyPublisher);

        Assert.Equal("subtext-ja", request.VocabularyName);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void BuildStartRequest_WithoutVocabularyName_LeavesItUnset(string? vocabularyName)
    {
        StartStreamTranscriptionRequest request =
            TranscribeStreamingClient.BuildStartRequest(Options(), vocabularyName, DummyPublisher);

        Assert.True(string.IsNullOrEmpty(request.VocabularyName));
    }
}
