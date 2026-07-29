using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

/// <summary>
/// <see cref="AudioTimeline"/> の純ロジック検証（音声時間 → 壁時計の写像）。
/// Transcribe の <c>StartTime</c> は送信済み音声の累積時間なので、keep-alive の無音注入
/// （FR-A1-05）がある経路では「セッション開始 + StartTime」では壁時計を復元できない。
/// </summary>
public sealed class AudioTimelineTests
{
    private const int SampleRate = 16_000;
    private static readonly DateTime T0 = new(2026, 6, 24, 1, 0, 0, DateTimeKind.Utc);

    /// <summary>指定秒数ぶんの 16bit mono PCM のバイト数。</summary>
    private static int Bytes(double seconds) => (int)(SampleRate * 2 * seconds);

    [Fact]
    public void ToWallClock_ReturnsNull_BeforeAnythingIsSent()
    {
        Assert.Null(new AudioTimeline(SampleRate).ToWallClock(TimeSpan.Zero));
    }

    [Fact]
    public void ToWallClock_MapsOffsetsWithinContinuousAudio()
    {
        var timeline = new AudioTimeline(SampleRate);
        timeline.Advance(Bytes(1), T0); // 音声 0.0〜1.0 秒
        timeline.Advance(Bytes(1), T0.AddSeconds(1)); // 音声 1.0〜2.0 秒

        Assert.Equal(T0.AddSeconds(1), timeline.ToWallClock(TimeSpan.FromSeconds(1)));
        Assert.Equal(T0.AddSeconds(1.5), timeline.ToWallClock(TimeSpan.FromSeconds(1.5)));
        Assert.Equal(T0, timeline.ToWallClock(TimeSpan.Zero)); // 基準点より前も線形に遡れる
    }

    [Fact]
    public void ToWallClock_DoesNotDriftAcrossKeepAliveSilence()
    {
        // 相手が 5 分黙っている間、WASAPI ループバックはフレームを出さず keep-alive の 20ms 無音だけが
        // 5 秒間隔で送られる（FR-A1-05）。音声時間は 5 分で 1.2 秒しか進まないため、
        // 「セッション開始 + StartTime」方式では captureUtc が約 5 分も過去へずれ、
        // self（連続してフレームが出る系統）との時系列インターリーブが壊れる。
        var timeline = new AudioTimeline(SampleRate);
        timeline.Advance(Bytes(1), T0); // 冒頭の 1 秒（音声 0.0〜1.0）

        const int rounds = 60; // 5 秒 × 60 = 5 分
        for (var i = 1; i <= rounds; i++)
        {
            timeline.Advance(Bytes(0.02), T0.AddSeconds(1 + (5 * i)));
        }

        // 無音明けの発話は「送信済み音声 = 1 + 60×0.02 秒」の位置から始まる。
        DateTime? mapped = timeline.ToWallClock(TimeSpan.FromSeconds(1 + (rounds * 0.02)));

        Assert.NotNull(mapped);
        DateTime expected = T0.AddSeconds(1 + (5 * rounds) + 0.02);
        Assert.True(
            Math.Abs((mapped.Value - expected).TotalSeconds) < 1.0,
            $"壁時計が約 {(expected - mapped.Value).TotalSeconds:F1} 秒ずれている（expected≈{expected:O} / actual={mapped:O}）");
    }
}
