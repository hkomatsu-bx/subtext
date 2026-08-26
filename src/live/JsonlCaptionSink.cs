using System.Globalization;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Subtext.Live;

/// <summary>
/// 確定字幕を JSONL（1 行 1 JSON・追記専用）へ書くシンク（B1F・FR-B1F-01/02）。
/// クラッシュ安全: 追記オープン・1 行ごとにディスクへ flush・既存行を書き換えない。
/// 稼働中の並行読取を許すため <see cref="FileShare.Read"/> で開く（Windows のロック衝突回避）。
/// スレッド安全（2 系統 self/others 並行）。本文はファイルのみ（NFR-B1F-02）。
/// </summary>
public sealed class JsonlCaptionSink : ICaptionSink
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        // ローカルファイル・非 HTML のため日本語をそのまま出す（可読性・サイズ）。読み手は json.loads で両対応。
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    private readonly FileStream _stream;
    private readonly StreamWriter _writer;
    // .NET 9 / C# 13 以降は専用の Lock 型を使う（IDE0330。同期用でないオブジェクトを
    // 誤ってロックする事故を防ぐ）。
    private readonly Lock _gate = new();

    public JsonlCaptionSink(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);
        string fullPath = Path.GetFullPath(path);
        string? dir = Path.GetDirectoryName(fullPath);
        if (!string.IsNullOrEmpty(dir))
        {
            Directory.CreateDirectory(dir);
        }

        // 追記専用・並行読取許可（FR-B1F-02）。
        _stream = new FileStream(fullPath, FileMode.Append, FileAccess.Write, FileShare.Read);
        _writer = new StreamWriter(_stream, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false))
        {
            NewLine = "\n", // LF 終端（連携契約）
        };
    }

    public void Append(LiveCaption caption, StreamSource source)
    {
        ArgumentNullException.ThrowIfNull(caption);
        string line = ToLine(caption, source);
        lock (_gate)
        {
            _writer.WriteLine(line);
            _writer.Flush();
            _stream.Flush(flushToDisk: true); // 各行をディスクへ確定（クラッシュ安全）
        }
    }

    /// <summary>1 確定字幕を JSONL の 1 行へシリアライズする（純粋・単体テスト対象, NFR-B1F-01）。</summary>
    public static string ToLine(LiveCaption caption, StreamSource source)
    {
        ArgumentNullException.ThrowIfNull(caption);
        var record = new CaptionRecord(
            OffsetMs: (long)Math.Round(caption.Offset.TotalMilliseconds, MidpointRounding.AwayFromZero),
            Source: source == StreamSource.Self ? "self" : "others",
            Speaker: source.Label(),
            Text: caption.Text,
            CaptureUtc: caption.CaptureUtc is { } utc
                ? utc.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ", CultureInfo.InvariantCulture)
                : null);
        return JsonSerializer.Serialize(record, JsonOptions);
    }

    public void Dispose()
    {
        lock (_gate)
        {
            _writer.Flush();
            _writer.Dispose(); // 下層 _stream も閉じる
        }
    }

    /// <summary>JSONL 1 行の連携契約（BR-B1F-IO-01）。camelCase で serialize する。</summary>
    private sealed record CaptionRecord(
        long OffsetMs,
        string Source,
        string Speaker,
        string Text,
        string? CaptureUtc);
}
