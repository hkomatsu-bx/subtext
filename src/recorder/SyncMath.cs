namespace Subtext.Recorder;

/// <summary>
/// L5 絶対時刻アンカー同期（Q2=A, BR-SYNC-01..03）の純粋計算。副作用なし＝単体テスト容易。
/// 「captureUtc を唯一の真実」とし、t0 起点の累積サンプル位置を算出して欠落を無音で補填する。
/// </summary>
public static class SyncMath
{
    public const int SampleRate = 16000;

    /// <summary>無音補填の発火閾値（BR-SYNC-03）。20ms 相当 = 320サンプル。微小ギャップは補填しない。</summary>
    public const long GapThresholdSamples = 320;

    /// <summary>指定時刻に「あるべき」累積サンプル数（t0 からの相対）。</summary>
    public static long ExpectedSamples(DateTime t0, DateTime captureUtc)
    {
        double seconds = (captureUtc - t0).TotalSeconds;
        if (seconds < 0)
        {
            seconds = 0;
        }

        return (long)Math.Round(seconds * SampleRate);
    }

    /// <summary>
    /// 現在の書込済サンプル数と、あるべき位置から、挿入すべき無音サンプル数を返す。
    /// 先頭フレームは t0 までの先行ギャップを必ず埋める。それ以降は閾値超のギャップのみ補填。
    /// 負のギャップ（微小な重なり/drift）は 0（連続書込で吸収, BR-SYNC-02）。
    /// </summary>
    public static long SilenceToInsert(long writtenSamples, long expectedSamples, bool isFirstFrame)
    {
        long gap = expectedSamples - writtenSamples;
        if (gap <= 0)
        {
            return 0;
        }

        if (isFirstFrame)
        {
            return gap;
        }

        return gap > GapThresholdSamples ? gap : 0;
    }
}
