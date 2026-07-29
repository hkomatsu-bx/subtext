using Subtext.Recorder;
using Xunit;

namespace Subtext.Recorder.Tests;

public sealed class SyncMathTests
{
    private static readonly DateTime T0 = new(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);

    [Fact]
    public void ExpectedSamples_OneSecond_Is16000()
    {
        long samples = SyncMath.ExpectedSamples(T0, T0.AddSeconds(1));
        Assert.Equal(16000, samples);
    }

    [Fact]
    public void ExpectedSamples_BeforeT0_ClampsToZero()
    {
        long samples = SyncMath.ExpectedSamples(T0, T0.AddSeconds(-1));
        Assert.Equal(0, samples);
    }

    [Fact]
    public void SilenceToInsert_FirstFrameWithLeadingGap_FillsEntireGap()
    {
        long silence = SyncMath.SilenceToInsert(writtenSamples: 0, expectedSamples: 5000, isFirstFrame: true);
        Assert.Equal(5000, silence);
    }

    [Fact]
    public void SilenceToInsert_MidStreamGapAboveThreshold_Fills()
    {
        long silence = SyncMath.SilenceToInsert(writtenSamples: 1600, expectedSamples: 17600, isFirstFrame: false);
        Assert.Equal(16000, silence);
    }

    [Fact]
    public void SilenceToInsert_MidStreamGapBelowThreshold_DoesNotFill()
    {
        // gap = 100 < 320(20ms 閾値) → 補填しない
        long silence = SyncMath.SilenceToInsert(writtenSamples: 1600, expectedSamples: 1700, isFirstFrame: false);
        Assert.Equal(0, silence);
    }

    [Fact]
    public void SilenceToInsert_NegativeGap_DoesNotFill()
    {
        // 微小な重なり/drift は連続書込で吸収（負ギャップは 0）
        long silence = SyncMath.SilenceToInsert(writtenSamples: 2000, expectedSamples: 1900, isFirstFrame: false);
        Assert.Equal(0, silence);
    }
}
