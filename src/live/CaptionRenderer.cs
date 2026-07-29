using System.Globalization;

namespace Subtext.Live;

/// <summary>
/// 字幕の表示と遅延計測（L4 / BR-RENDER / BR-LAT）。partial/final いずれも新規行で出力し（Q2=B）、
/// capture 時刻起点の遅延を算出・表示する（Q3=A）。final を観測サンプルとして集計する（FR-14）。
/// 字幕テキストは永続化しない（コンソールのみ・NFR-SEC-04）。スレッド安全（2系統並行表示, Q1=A）。
/// </summary>
public sealed class CaptionRenderer
{
    private readonly TextWriter _out;
    private readonly Func<DateTime> _now;
    private readonly List<LatencySample> _samples = new();
    private readonly object _gate = new();

    public CaptionRenderer(TextWriter? output = null, Func<DateTime>? now = null)
    {
        _out = output ?? Console.Out;
        _now = now ?? (() => DateTime.UtcNow);
    }

    /// <summary>観測済み final サンプル（読み取り専用スナップショット）。</summary>
    public IReadOnlyList<LatencySample> Samples
    {
        get
        {
            lock (_gate)
            {
                return _samples.ToArray();
            }
        }
    }

    /// <summary>1字幕を表示し、必要なら遅延を観測する。</summary>
    public void Render(LiveCaption caption, StreamSource source)
    {
        ArgumentNullException.ThrowIfNull(caption);

        double? latencyMs = caption.CaptureUtc is { } captured
            ? (_now() - captured).TotalMilliseconds
            : null;

        string line = FormatLine(caption, latencyMs);
        lock (_gate)
        {
            _out.WriteLine(line);
            if (!caption.IsPartial && latencyMs is { } ms)
            {
                _samples.Add(new LatencySample(source, caption.Offset, ms));
            }
        }
    }

    /// <summary>
    /// 再接続時に当該系統の進行中状態をリセットする（FR-A1-06）。本描画器は partial を逐次新規行で
    /// 出力しバッファしないため破棄すべき内部 partial は持たない。よって境界マーカーを出力して
    /// 新旧セッションの offset/partial 混線を読み手に明示するに留め、確定 final の集計（遅延サマリ）は保持する。
    /// 字幕テキストは出さない（NFR-SEC-04）。
    /// </summary>
    public void Reset(StreamSource source)
    {
        lock (_gate)
        {
            _out.WriteLine($"[{source.Label()}] --- 再接続（以降は新セッションの相対時刻） ---");
        }
    }

    /// <summary>セッション終了時の遅延サマリ（件数・平均・最大）。観測が無ければ注記のみ。</summary>
    public string BuildLatencySummary()
    {
        lock (_gate)
        {
            if (_samples.Count == 0)
            {
                return "遅延サマリ: 確定字幕の観測サンプルなし。";
            }

            double avg = _samples.Average(s => s.LatencyMs);
            double max = _samples.Max(s => s.LatencyMs);
            return string.Format(
                CultureInfo.InvariantCulture,
                "遅延サマリ: final {0}件 / 平均 {1:F0}ms / 最大 {2:F0}ms",
                _samples.Count, avg, max);
        }
    }

    /// <summary>表示行を整形する（純粋）。partial は `~` 印、final は ` `。系統ラベル・offset・遅延を併記。</summary>
    public static string FormatLine(LiveCaption caption, double? latencyMs)
    {
        string mark = caption.IsPartial ? "~" : " ";
        string offset = FormatClock(caption.Offset);
        string latency = latencyMs is { } ms
            ? string.Format(CultureInfo.InvariantCulture, "  (Δ{0:F0}ms)", ms)
            : string.Empty;
        return $"[{offset}] [{caption.Speaker}]{mark} {caption.Text}{latency}";
    }

    private static string FormatClock(TimeSpan offset)
    {
        int total = (int)Math.Max(0, offset.TotalSeconds);
        return string.Format(CultureInfo.InvariantCulture, "{0:00}:{1:00}", total / 60, total % 60);
    }
}
