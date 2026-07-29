using System.Text.Json;
using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

/// <summary>B1F: 確定字幕 JSONL シンクのテスト。ToLine は純粋（AWS/実機不要）。</summary>
public sealed class JsonlCaptionSinkTests
{
    [Fact]
    public void ToLine_SerializesAllFields()
    {
        var caption = new LiveCaption(
            "相手", "こんにちは", IsPartial: false, TimeSpan.FromMilliseconds(1500),
            new DateTime(2026, 7, 12, 4, 1, 46, 123, DateTimeKind.Utc));

        string line = JsonlCaptionSink.ToLine(caption, StreamSource.Others);

        using var doc = JsonDocument.Parse(line);
        JsonElement root = doc.RootElement;
        Assert.Equal(1500, root.GetProperty("offsetMs").GetInt64());
        Assert.Equal("others", root.GetProperty("source").GetString());
        Assert.Equal("相手", root.GetProperty("speaker").GetString());
        Assert.Equal("こんにちは", root.GetProperty("text").GetString());
        Assert.Equal("2026-07-12T04:01:46.123Z", root.GetProperty("captureUtc").GetString());
    }

    [Fact]
    public void ToLine_SelfSource_UsesSelfKeyAndLabel()
    {
        var caption = new LiveCaption("自分", "はい", IsPartial: false, TimeSpan.Zero);

        using var doc = JsonDocument.Parse(JsonlCaptionSink.ToLine(caption, StreamSource.Self));
        Assert.Equal("self", doc.RootElement.GetProperty("source").GetString());
        Assert.Equal("自分", doc.RootElement.GetProperty("speaker").GetString());
    }

    [Fact]
    public void ToLine_NullCaptureUtc_WritesJsonNull()
    {
        var caption = new LiveCaption("自分", "x", IsPartial: false, TimeSpan.Zero, CaptureUtc: null);

        using var doc = JsonDocument.Parse(JsonlCaptionSink.ToLine(caption, StreamSource.Self));
        Assert.Equal(JsonValueKind.Null, doc.RootElement.GetProperty("captureUtc").ValueKind);
    }

    [Fact]
    public void ToLine_EscapesSpecialCharacters_AndStaysSingleLine()
    {
        var caption = new LiveCaption(
            "自分", "引用\"と\\改行\nと<tag>", IsPartial: false, TimeSpan.Zero);

        string line = JsonlCaptionSink.ToLine(caption, StreamSource.Self);

        Assert.DoesNotContain('\n', line); // 改行はエスケープされ 1 行を保つ（JSONL 不変条件）
        using var doc = JsonDocument.Parse(line);
        Assert.Equal("引用\"と\\改行\nと<tag>", doc.RootElement.GetProperty("text").GetString());
    }

    [Fact]
    public void Append_WritesLine_AndAllowsConcurrentRead()
    {
        string path = Path.Combine(Path.GetTempPath(), $"subtext-b1f-{Guid.NewGuid():N}.jsonl");
        try
        {
            using var sink = new JsonlCaptionSink(path);
            sink.Append(new LiveCaption("自分", "一行目", IsPartial: false, TimeSpan.Zero), StreamSource.Self);

            // 稼働中（sink オープン中）に別ハンドルで読める＝Windows のロック衝突なし（FR-B1F-02）。
            using var reader = new StreamReader(
                new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite));
            string? first = reader.ReadLine();

            Assert.NotNull(first);
            using var doc = JsonDocument.Parse(first!);
            Assert.Equal("一行目", doc.RootElement.GetProperty("text").GetString());
        }
        finally
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
    }
}
