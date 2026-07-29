using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

public sealed class LiveModelsTests
{
    [Theory]
    [InlineData(StreamSource.Self, "自分")]
    [InlineData(StreamSource.Others, "相手")]
    public void Label_MapsSourceToJapaneseLabel(StreamSource source, string expected)
    {
        Assert.Equal(expected, source.Label());
    }

    [Fact]
    public void StreamingOptions_ForSource_CarriesParameters()
    {
        StreamingOptions opts = StreamingOptions.ForSource(StreamSource.Others, "ja-JP", 16000);
        Assert.Equal(StreamSource.Others, opts.Source);
        Assert.Equal("ja-JP", opts.LanguageCode);
        Assert.Equal(16000, opts.SampleRate);
    }
}

public sealed class LiveSttConfigTests
{
    private static string WriteAppSettings(string json)
    {
        string dir = Path.Combine(Path.GetTempPath(), "subtext-live-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        File.WriteAllText(Path.Combine(dir, "appsettings.json"), json);
        return dir;
    }

    [Fact]
    public void Load_ParsesValidConfig()
    {
        string dir = WriteAppSettings(
            """
            { "Live": { "AwsRegion": "ap-northeast-1", "Language": "ja-JP", "Sources": ["self","others"], "SampleRate": 16000 } }
            """);

        LiveSttConfig config = LiveSttConfig.Load(dir);

        Assert.Equal("ap-northeast-1", config.AwsRegion);
        Assert.Equal(new[] { StreamSource.Self, StreamSource.Others }, config.Sources);
        Assert.Equal(16000, config.SampleRate);
    }

    [Fact]
    public void Load_MissingRegion_Throws()
    {
        string dir = WriteAppSettings("""{ "Live": { "Language": "ja-JP" } }""");
        Assert.Throws<InvalidOperationException>(() => LiveSttConfig.Load(dir));
    }

    [Fact]
    public void Load_UnknownSource_Throws()
    {
        string dir = WriteAppSettings(
            """{ "Live": { "AwsRegion": "ap-northeast-1", "Language": "ja-JP", "Sources": ["bogus"] } }""");
        Assert.Throws<InvalidOperationException>(() => LiveSttConfig.Load(dir));
    }

    [Fact]
    public void Load_EmptySources_DefaultsToBoth()
    {
        string dir = WriteAppSettings(
            """{ "Live": { "AwsRegion": "ap-northeast-1", "Language": "ja-JP" } }""");
        LiveSttConfig config = LiveSttConfig.Load(dir);
        Assert.Equal(new[] { StreamSource.Self, StreamSource.Others }, config.Sources);
    }

    [Fact]
    public void Load_VocabularyName_IsBound()
    {
        string dir = WriteAppSettings(
            """{ "Live": { "AwsRegion": "ap-northeast-1", "Language": "ja-JP", "VocabularyName": "subtext-ja" } }""");
        LiveSttConfig config = LiveSttConfig.Load(dir);
        Assert.Equal("subtext-ja", config.VocabularyName);
    }

    [Fact]
    public void Load_NoVocabularyName_IsNull()
    {
        string dir = WriteAppSettings(
            """{ "Live": { "AwsRegion": "ap-northeast-1", "Language": "ja-JP" } }""");
        LiveSttConfig config = LiveSttConfig.Load(dir);
        Assert.Null(config.VocabularyName);
    }

    [Fact]
    public void Load_BlankVocabularyName_IsNull()
    {
        string dir = WriteAppSettings(
            """{ "Live": { "AwsRegion": "ap-northeast-1", "Language": "ja-JP", "VocabularyName": "   " } }""");
        LiveSttConfig config = LiveSttConfig.Load(dir);
        Assert.Null(config.VocabularyName);
    }
}
