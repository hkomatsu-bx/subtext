namespace Subtext.Live;

/// <summary>
/// 送信済み音声の「音声時間 → 壁時計」対応（BR-B1F-TIME-01 の前提）。
///
/// Transcribe が返す <c>result.StartTime</c> は **送信した音声の累積時間**であって壁時計ではない。
/// FR-A1-05 の keep-alive は capture が途絶している間 5 秒あたり 20ms しか音声を送らないため、
/// ループバックが静かな間（相手が黙っている通常状態）は音声時間が壁時計の 0.4% しか進まない。
/// 「セッション開始時刻 + StartTime」で captureUtc を作ると無音 1 分で約 60 秒遅れ、10 分で
/// 約 10 分ずれる。マイク側（self）はフレームが連続するためほとんどずれないので、系統間の
/// インターリーブが壊れ（live_to_vtt は captureUtc 昇順で並べる）、遅延表示も無意味になる。
///
/// ここでは「最後に送ったチャンク」だけを基準点として保持し、そこからの相対で写す。基準点は
/// 書き込みごとに更新するので無音区間で開いた差はその都度解消され、誤差は「連続して音声が
/// 流れている区間の中」に収まる。履歴を持たないためメモリは定数。
/// </summary>
internal sealed class AudioTimeline
{
    private readonly double _bytesPerSecond;
    // .NET 9 / C# 13 以降は専用の Lock 型を使う（object ロックより速く、同期用でないオブジェクトを
    // 誤ってロックする事故も防げる。IDE0330）。音声チャンクごとに通る経路なので効きやすい。
    private readonly Lock _gate = new();

    private double _sentSeconds; // 送信済み音声の累積秒
    private double _anchorAudioSeconds; // 基準点チャンクの開始位置（音声時間）
    private DateTime? _anchorWallUtc; // 基準点チャンクの壁時計

    /// <param name="sampleRate">送信 PCM のサンプリングレート（Hz）。</param>
    /// <param name="bytesPerSample">1 サンプルのバイト数（16bit mono = 2）。</param>
    public AudioTimeline(int sampleRate, int bytesPerSample = 2)
    {
        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(sampleRate);
        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(bytesPerSample);
        _bytesPerSecond = sampleRate * (double)bytesPerSample;
    }

    /// <summary>
    /// 送信した PCM チャンクを記録する。<paramref name="wallUtc"/> はそのチャンクの実時刻
    /// （実フレームは捕捉時刻、keep-alive の無音は注入時刻）。
    /// </summary>
    public void Advance(int pcmBytes, DateTime wallUtc)
    {
        if (pcmBytes <= 0)
        {
            return;
        }

        lock (_gate)
        {
            _anchorAudioSeconds = _sentSeconds;
            _anchorWallUtc = wallUtc;
            _sentSeconds += pcmBytes / _bytesPerSecond;
        }
    }

    /// <summary>
    /// 音声時間のオフセットを壁時計へ写す。まだ何も送っていなければ null（従来と同じ扱い）。
    /// 基準点より前のオフセットは線形に遡る（音声が流れている区間では両者の進みが一致する）。
    /// </summary>
    public DateTime? ToWallClock(TimeSpan audioOffset)
    {
        lock (_gate)
        {
            return _anchorWallUtc is { } anchor
                ? anchor + TimeSpan.FromSeconds(audioOffset.TotalSeconds - _anchorAudioSeconds)
                : null;
        }
    }
}
