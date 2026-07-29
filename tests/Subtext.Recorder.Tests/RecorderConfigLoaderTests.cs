using Subtext.Recorder;
using Xunit;

namespace Subtext.Recorder.Tests;

// FR-15: 設定束縛の優先順位（CLI 引数 > 環境変数 > appsettings.json > 既定値）を検証する。
// appsettings.json の混入を避けるため、存在しないファイル名を渡して JSON 段を無効化する。
public sealed class RecorderConfigLoaderTests
{
    private const string NoJsonFile = "appsettings.does-not-exist.json";

    [Fact]
    public void Load_UsesDefaults_WhenNoArgsAndNoFile()
    {
        RecorderConfig config = RecorderConfigLoader.Load(args: null, fileName: NoJsonFile);

        Assert.Equal(180, config.MaxDurationMinutes);
        Assert.Equal("recordings", config.OutputDir);
        Assert.Null(config.StopFile);
    }

    [Fact]
    public void Load_NonPositiveMaxDuration_Throws()
    {
        var ex = Assert.Throws<InvalidOperationException>(() =>
            RecorderConfigLoader.Load(new[] { "--minutes", "0" }, NoJsonFile));

        Assert.Contains("MaxDurationMinutes", ex.Message);
    }

    [Fact]
    public void Load_BlankOutputDir_Throws()
    {
        var ex = Assert.Throws<InvalidOperationException>(() =>
            RecorderConfigLoader.Load(new[] { "--out", "  " }, NoJsonFile));

        Assert.Contains("OutputDir", ex.Message);
    }

    [Fact]
    public void Load_BindsShortSwitch_ForMaxDurationMinutes()
    {
        RecorderConfig config = RecorderConfigLoader.Load(
            new[] { "--minutes", "2" }, NoJsonFile);

        Assert.Equal(2, config.MaxDurationMinutes);
    }

    [Fact]
    public void Load_BindsMultipleSwitches_IncludingStopFileAndOut()
    {
        RecorderConfig config = RecorderConfigLoader.Load(
            new[] { "-m", "5", "--out", "out-dir", "--stop-file", "out-dir/.stop" },
            NoJsonFile);

        Assert.Equal(5, config.MaxDurationMinutes);
        Assert.Equal("out-dir", config.OutputDir);
        Assert.Equal("out-dir/.stop", config.StopFile);
    }

    [Fact]
    public void Load_BindsFullKeyForm_WithoutSwitchMapping()
    {
        RecorderConfig config = RecorderConfigLoader.Load(
            new[] { "--Recorder:MaxDurationMinutes=7" }, NoJsonFile);

        Assert.Equal(7, config.MaxDurationMinutes);
    }

    [Fact]
    public void Load_CommandLineOverridesEnvironmentVariable()
    {
        const string envKey = "SUBTEXT_Recorder__MaxDurationMinutes";
        string? original = Environment.GetEnvironmentVariable(envKey);
        try
        {
            Environment.SetEnvironmentVariable(envKey, "99");

            // env=99 だが CLI=5 が後勝ちで上書きする。
            RecorderConfig config = RecorderConfigLoader.Load(
                new[] { "--minutes", "5" }, NoJsonFile);

            Assert.Equal(5, config.MaxDurationMinutes);
        }
        finally
        {
            Environment.SetEnvironmentVariable(envKey, original);
        }
    }

    [Fact]
    public void Load_FallsBackToEnvironmentVariable_WhenNoCliArg()
    {
        const string envKey = "SUBTEXT_Recorder__MaxDurationMinutes";
        string? original = Environment.GetEnvironmentVariable(envKey);
        try
        {
            Environment.SetEnvironmentVariable(envKey, "42");

            RecorderConfig config = RecorderConfigLoader.Load(args: null, fileName: NoJsonFile);

            Assert.Equal(42, config.MaxDurationMinutes);
        }
        finally
        {
            Environment.SetEnvironmentVariable(envKey, original);
        }
    }
}
