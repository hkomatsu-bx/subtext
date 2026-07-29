using System.Text;
using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

public sealed class CaptionRendererTests
{
    [Fact]
    public void FormatLine_FinalCaption_HasNoPartialMark()
    {
        var caption = new LiveCaption("自分", "こんにちは", IsPartial: false, TimeSpan.FromSeconds(3));
        string line = CaptionRenderer.FormatLine(caption, latencyMs: null);
        Assert.Equal("[00:03] [自分]  こんにちは", line);
    }

    [Fact]
    public void FormatLine_PartialCaption_HasTilde()
    {
        var caption = new LiveCaption("相手", "ええと", IsPartial: true, TimeSpan.FromSeconds(65));
        string line = CaptionRenderer.FormatLine(caption, latencyMs: null);
        Assert.Equal("[01:05] [相手]~ ええと", line);
    }

    [Fact]
    public void FormatLine_WithLatency_AppendsDelta()
    {
        var caption = new LiveCaption("自分", "はい", IsPartial: false, TimeSpan.Zero);
        string line = CaptionRenderer.FormatLine(caption, latencyMs: 240.0);
        Assert.EndsWith("(Δ240ms)", line);
    }

    [Fact]
    public void Render_FinalCaption_RecordsLatencySample()
    {
        // Arrange: capture から 200ms 後に確定したとみなす。
        var captured = new DateTime(2026, 6, 20, 1, 0, 0, DateTimeKind.Utc);
        var output = new StringWriter(new StringBuilder());
        var renderer = new CaptionRenderer(output, now: () => captured.AddMilliseconds(200));
        var caption = new LiveCaption("相手", "了解です", IsPartial: false, TimeSpan.FromSeconds(1), captured);

        // Act
        renderer.Render(caption, StreamSource.Others);

        // Assert
        Assert.Single(renderer.Samples);
        Assert.Equal(200, renderer.Samples[0].LatencyMs, precision: 0);
        Assert.Contains("(Δ200ms)", output.ToString());
    }

    [Fact]
    public void Render_PartialCaption_DoesNotRecordSample()
    {
        var captured = new DateTime(2026, 6, 20, 1, 0, 0, DateTimeKind.Utc);
        var renderer = new CaptionRenderer(new StringWriter(), now: () => captured.AddMilliseconds(100));
        var caption = new LiveCaption("相手", "えー", IsPartial: true, TimeSpan.Zero, captured);

        renderer.Render(caption, StreamSource.Others);

        Assert.Empty(renderer.Samples); // partial は観測対象外（BR-LAT-02）
    }

    [Fact]
    public void BuildLatencySummary_NoSamples_NotesAbsence()
    {
        var renderer = new CaptionRenderer(new StringWriter());
        Assert.Contains("サンプルなし", renderer.BuildLatencySummary());
    }

    [Fact]
    public void BuildLatencySummary_WithSamples_ReportsCountAndStats()
    {
        var captured = new DateTime(2026, 6, 20, 1, 0, 0, DateTimeKind.Utc);
        int call = 0;
        // 1件目 +100ms, 2件目 +300ms
        var renderer = new CaptionRenderer(new StringWriter(),
            now: () => captured.AddMilliseconds(call++ == 0 ? 100 : 300));
        renderer.Render(new LiveCaption("自分", "a", false, TimeSpan.Zero, captured), StreamSource.Self);
        renderer.Render(new LiveCaption("自分", "b", false, TimeSpan.FromSeconds(1), captured), StreamSource.Self);

        string summary = renderer.BuildLatencySummary();

        Assert.Contains("final 2件", summary);
        Assert.Contains("最大 300ms", summary);
    }
}
