using Subtext.Capture;

namespace Subtext.Recorder;

/// <summary>
/// L5 同期書込＋L7 メタ生成（FR-03/04, BR-SYNC-*, BR-IO-*）。2系統を各自のタイムラインで
/// t0 起点の絶対位置へ書き込み（互いに待たせない）、欠落は無音で補填する。
/// 異常時は fail-fast＋部分保存（Q7=A）: 片系統の障害で両系統を停止し、書込済みを確定後に
/// RecordingException を送出する。マニフェスト/メタは常に出力される。
/// </summary>
/// <param name="normalizer">L4 正規化器。</param>
/// <param name="warn">品質警告の出力先（BR-QLT-03）。null なら警告を捨てる。CLI は stderr を渡す。</param>
public sealed class SyncRecorder(AudioNormalizer normalizer, Action<string>? warn = null)
{
    private readonly AudioNormalizer _normalizer = normalizer ?? throw new ArgumentNullException(nameof(normalizer));
    private readonly Action<string>? _warn = warn;

    /// <summary>
    /// 2系統を録音し、WAV・メタ・マニフェストを出力する。
    /// </summary>
    /// <param name="startUtcOverride">t0 の上書き（テスト用の seam）。null なら現在時刻。</param>
    public async Task<RecordingManifest> RecordAsync(
        IAsyncEnumerable<AudioFrame> selfStream,
        IAsyncEnumerable<AudioFrame> othersStream,
        RecorderOptions options,
        CancellationToken ct,
        DateTime? startUtcOverride = null)
    {
        ArgumentNullException.ThrowIfNull(selfStream);
        ArgumentNullException.ThrowIfNull(othersStream);
        ArgumentNullException.ThrowIfNull(options);

        string sessionDir = Path.Combine(options.OutputDir, options.SessionId);

        // BR-IO-01: 既存セッションへの上書きを防ぐ。同一秒での多重起動や同一 sessionId の再実行を
        // 録音開始前に止める（録音済みの WAV を黙って壊さない）。パスは message に載せない（BR-ERR-04）。
        if (Directory.Exists(sessionDir))
        {
            throw new InvalidOperationException(
                $"Output session already exists: {options.SessionId} (overwriting a recording is not allowed).");
        }

        Directory.CreateDirectory(sessionDir);
        DateTime t0 = startUtcOverride ?? DateTime.UtcNow;

        string selfWavPath = Path.Combine(sessionDir, "self.wav");
        string othersWavPath = Path.Combine(sessionDir, "others.wav");

        using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct);
        Exception? fault = null;

        var selfSink = new StreamSink(selfWavPath, _normalizer, t0, StreamRole.Self, _warn);
        var othersSink = new StreamSink(othersWavPath, _normalizer, t0, StreamRole.Others, _warn);

        async Task Pump(IAsyncEnumerable<AudioFrame> stream, StreamSink sink)
        {
            try
            {
                await foreach (var frame in stream.WithCancellation(linked.Token).ConfigureAwait(false))
                {
                    sink.Process(frame);
                }
            }
            catch (OperationCanceledException)
            {
                // 手動停止・上限到達は正常停止（Q1=B）。
            }
            catch (Exception ex)
            {
                // 致命的異常: fail-fast。最初の原因のみ記録し（2系統同時障害の競合対策）、相手系統も停止させる（Q7=A）。
                Interlocked.CompareExchange(ref fault, ex, null);
                linked.Cancel();
            }
        }

        SidecarMeta selfMeta;
        SidecarMeta othersMeta;
        Exception? selfFinishError;
        Exception? othersFinishError;
        try
        {
            await Task.WhenAll(
                Pump(selfStream, selfSink),
                Pump(othersStream, othersSink)).ConfigureAwait(false);
        }
        finally
        {
            // 正常・異常に関わらず両 WAV を確定して部分保存を保証する。片系統の確定失敗
            // （ディスクフル等）でも他系統の確定とマニフェスト出力は必ず行う。
            (selfMeta, selfFinishError) = selfSink.Finish(options.SelfDevice.Name);
            (othersMeta, othersFinishError) = othersSink.Finish(options.OthersDevice.Name);
        }

        fault ??= selfFinishError ?? othersFinishError;

        // BR-QLT-03: 相手系統が全区間無音なら警告する。ループバック先のデバイス取り違えや
        // 会議音声が別デバイスへ出ていた場合を、議事録段まで持ち込ませない。
        if (!othersSink.HasSignal)
        {
            _warn?.Invoke(
                "WARN: 相手系統(others)は全区間無音でした。ループバック元のデバイス指定を確認してください（BR-QLT-03）。");
        }

        RecordingStatus status = fault is null ? RecordingStatus.Complete : RecordingStatus.Incomplete;
        var manifest = new RecordingManifest(
            options.SessionId,
            DateTime.UtcNow,
            [selfMeta, othersMeta],
            status,
            t0);

        try
        {
            File.WriteAllText(Path.Combine(sessionDir, "self.meta.json"), RecordingJson.Serialize(selfMeta));
            File.WriteAllText(Path.Combine(sessionDir, "others.meta.json"), RecordingJson.Serialize(othersMeta));
            File.WriteAllText(Path.Combine(sessionDir, "manifest.json"), RecordingJson.Serialize(manifest));
        }
        catch (Exception ex)
        {
            // メタ/マニフェスト出力の失敗も録音異常として扱う（WAV は確定済み＝部分保存）。
            // 録音中の fault があれば失わずに束ねる。
            throw new RecordingException(
                "Recording metadata could not be written; WAV files are finalized (partial save).",
                fault is null ? ex : new AggregateException(fault, ex));
        }

        if (fault is not null)
        {
            throw new RecordingException(
                "Recording failed during capture; partial data saved (status=incomplete).",
                fault);
        }

        return manifest;
    }

    /// <summary>
    /// 1系統分の同期書込状態。WAV へ絶対位置で書き、無音補填量を集計する。
    /// </summary>
    private sealed class StreamSink
    {
        /// <summary>1 回のギャップ補填を「長すぎる」と警告する閾値（サンプル）。5 分相当。
        ///
        /// 単一の連続ギャップがこの長さになるのは、時計の跳躍（NTP のステップ補正）・スリープ復帰・
        /// デバイス停止のいずれか。補填自体は 2 系統の絶対位置を合わせるために必要なので止めないが、
        /// 黙って WAV と **Transcribe の課金対象** が同じだけ伸びるため操作者へ知らせる
        /// （5 分の録音が 65 分ぶん課金される、という事故を見えるようにする。BR-QLT-03 と同旨）。</summary>
        private const long LongGapWarnSamples = 5 * 60 * SyncMath.SampleRate;

        private readonly WavWriter _wav;
        private readonly AudioNormalizer _normalizer;
        private readonly DateTime _t0;
        private readonly StreamRole _role;
        private readonly Action<string>? _warn;

        private long _writtenSamples;
        private long _silenceFilledSamples;
        private DateTime _startTimeUtc;
        private bool _isFirstFrame = true;
        private bool _longGapWarned;
        private ResampleState _resampleState = ResampleState.Initial; // リサンプル連続性は系統ごとに保持

        /// <summary>正規化後に 0 以外のサンプルを 1 つでも書いたか（BR-QLT-03 の全区間無音判定）。</summary>
        public bool HasSignal { get; private set; }

        public StreamSink(
            string wavPath,
            AudioNormalizer normalizer,
            DateTime t0,
            StreamRole role,
            Action<string>? warn)
        {
            _wav = new WavWriter(wavPath);
            _normalizer = normalizer;
            _t0 = t0;
            _role = role;
            _warn = warn;
            _startTimeUtc = t0;
        }

        public void Process(AudioFrame raw)
        {
            (AudioFrame nf, _resampleState) = _normalizer.Normalize(raw, _resampleState);
            long expected = SyncMath.ExpectedSamples(_t0, nf.CaptureUtc);
            long silence = SyncMath.SilenceToInsert(_writtenSamples, expected, _isFirstFrame);

            if (silence > 0)
            {
                _wav.WriteSilence(silence);
                _writtenSamples += silence;

                // 先頭の先行ギャップは開始オフセットであり「欠落補填」には数えない（BR-SYNC-05）。
                if (!_isFirstFrame)
                {
                    _silenceFilledSamples += silence;
                    WarnIfGapTooLong(silence);
                }
            }

            if (_isFirstFrame)
            {
                _startTimeUtc = nf.CaptureUtc;
                _isFirstFrame = false;
            }

            // 無音補填分は信号に数えない。0 以外を一度見たら以降の走査は省く（BR-QLT-03）。
            if (!HasSignal && nf.Pcm.AsSpan().IndexOfAnyExcept((byte)0) >= 0)
            {
                HasSignal = true;
            }

            _wav.WritePcm(nf.Pcm);
            _writtenSamples += nf.Pcm.Length / 2; // 16bit mono = 2 bytes/sample
        }

        /// <summary>WAV を確定・解放し、メタを返す。確定失敗でもハンドルは必ず解放し、
        /// メタは書込済みカウンタから常に構築する（エラーは呼出側で incomplete 判定に用いる）。</summary>
        /// <summary>長すぎる単一ギャップを 1 回だけ警告する（連投で他の警告を埋めない）。</summary>
        private void WarnIfGapTooLong(long silence)
        {
            if (_longGapWarned || silence <= LongGapWarnSamples)
            {
                return;
            }

            _longGapWarned = true;
            double seconds = silence / (double)SyncMath.SampleRate;
            _warn?.Invoke(
                $"WARN: {_role} 系統で {seconds:F0} 秒の連続ギャップを無音で補填しました"
                + "（時計の跳躍・スリープ復帰・デバイス停止の可能性）。"
                + "WAV と Transcribe の課金対象が同じだけ伸びます。");
        }

        public (SidecarMeta Meta, Exception? Error) Finish(string deviceName)
        {
            Exception? error = null;
            try
            {
                _wav.Finish();
            }
            catch (Exception ex)
            {
                error = ex;
            }
            finally
            {
                try
                {
                    _wav.Dispose();
                }
                catch (Exception ex)
                {
                    error ??= ex;
                }
            }

            return (BuildMeta(deviceName), error);
        }

        private SidecarMeta BuildMeta(string deviceName)
        {
            return new SidecarMeta(
                WavPath: _role == StreamRole.Self ? "self.wav" : "others.wav",
                StreamRole: _role,
                StartTimeUtc: _startTimeUtc,
                SampleRate: SyncMath.SampleRate,
                Channels: 1,
                BitDepth: 16,
                DeviceName: deviceName,
                DurationSec: _writtenSamples / (double)SyncMath.SampleRate,
                SilenceFilledSec: _silenceFilledSamples / (double)SyncMath.SampleRate);
        }
    }
}
