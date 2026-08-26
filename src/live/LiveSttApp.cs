using Subtext.Capture;

namespace Subtext.Live;

/// <summary>
/// ライブ字幕オーケストレータ（L1→L4 駆動）。設定された系統（既定 self/others, Q1=A）を
/// 並行にストリーミングし、字幕を表示する。各系統は<b>互いに独立</b>して動き、一過性の切断からは
/// 自律再接続する（FR-A1-01,02,03,07）。恒久障害・再接続予算超過の系統だけを停止し、他系統は継続する
/// （D1。旧 fail-fast 全体停止は撤廃）。全系統共通の停止信号は ct（Ctrl+C）のみ。
/// 録音・正規化・同期は持たない（capture 責務）。
/// </summary>
/// <param name="clock">現在時刻の取得。再接続の決定化 seam（本番は実時刻）。</param>
/// <param name="jitter">バックオフのジッタ [0,1)。決定化 seam（本番は実乱数）。</param>
/// <param name="sink">確定字幕の永続化先（B1F）。null なら永続化しない＝従来挙動。所有権は呼び出し側（Dispose）。</param>
public sealed class LiveSttApp(
    IAudioCapture capture,
    ILiveTranscribeClient transcribe,
    CaptionRenderer renderer,
    LiveSttConfig config,
    Func<DateTime>? clock = null,
    Func<double>? jitter = null,
    ICaptionSink? sink = null)
{
    private readonly IAudioCapture _capture = capture ?? throw new ArgumentNullException(nameof(capture));
    private readonly ILiveTranscribeClient _transcribe =
        transcribe ?? throw new ArgumentNullException(nameof(transcribe));
    private readonly CaptionRenderer _renderer = renderer ?? throw new ArgumentNullException(nameof(renderer));
    private readonly LiveSttConfig _config = config ?? throw new ArgumentNullException(nameof(config));
    private readonly Func<DateTime> _clock = clock ?? (() => DateTime.UtcNow);
    private readonly Func<double> _jitter = jitter ?? (() => Random.Shared.NextDouble());
    private readonly ICaptionSink? _sink = sink;

    /// <summary>
    /// 全系統を並行にストリーミングする。各系統は独立して再接続し、恒久障害/予算超過の系統のみ終了する。
    /// 全系統が終了（恒久障害 or ct 停止）するまで稼働する（FR-A1-07）。Ctrl+C 等で ct がキャンセルされると
    /// 全系統が協調終了する。
    /// </summary>
    public async Task RunAsync(CancellationToken ct)
    {
        // デバイス一覧は接続のたびに取り直す（SuperviseStreamAsync 内）。ここで固定すると、
        // 会議中に既定デバイスが切り替わった（イヤホンの着脱・会議アプリのデバイス変更）あとの
        // 再接続が消えたデバイスを開き続け、**エラーも出さずに字幕が止まる**。
        // 系統は互いに止めない（D1）。ct のみが全系統共通の停止信号。
        var tasks = _config.Sources
            .Select(source => SuperviseStreamAsync(source, ct))
            .ToArray();

        try
        {
            await Task.WhenAll(tasks).ConfigureAwait(false);
        }
        finally
        {
            string summary = _renderer.BuildLatencySummary();
            Console.Out.WriteLine(summary); // 遅延の最小サマリ（SC-P4 判断材料, 非永続）
        }
    }

    /// <summary>
    /// 1 系統の生涯を監督する。transient な切断は指数バックオフで再接続し、fatal もしくは
    /// 再接続予算（ウィンドウ）超過なら<b>当該系統のみ</b>終了する（D1 / BR-RECONN-01,02,03）。
    /// </summary>
    private async Task SuperviseStreamAsync(
        StreamSource source,
        CancellationToken ct)
    {
        DateTime? windowStartUtc = null; // 連続失敗ウィンドウの起点（初回失敗で確定）
        int attempt = 0;

        while (!ct.IsCancellationRequested)
        {
            bool progressed = false; // このセッションで1件でも字幕を受信したか
            try
            {
                // 接続のたびに現在のデバイス一覧を取り直す（既定デバイスの切替に追従する）。
                IReadOnlyList<AudioDevice> devices = _capture.ListDevices();
                await RunOneSessionAsync(source, devices, () => progressed = true, ct).ConfigureAwait(false);
                return; // capture 正常終端 → 系統正常終了
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                return; // ct による正常停止（Ctrl+C）
            }
            catch (Exception ex)
            {
                DiagnoseFault(source, ex); // 任意診断（既定OFF。SUBTEXT_DIAG_RECONNECT=1 で原因を stderr へ）

                // 字幕受信に成功していた接続が落ちた場合は予算をリセット（BR-RECONN-03）。
                if (progressed)
                {
                    windowStartUtc = null;
                    attempt = 0;
                }

                if (ReconnectPolicy.Classify(ex) == FaultKind.Fatal)
                {
                    LogStop(source, permanent: true, reason: "fatal");
                    return; // 当該系統のみ停止（D1）
                }

                windowStartUtc ??= _clock();
                if (ReconnectPolicy.IsBudgetExhausted(windowStartUtc.Value, _clock(), _config.Reconnect))
                {
                    LogStop(source, permanent: true, reason: "budget-exhausted");
                    return; // 予算超過＝恒久障害（当該系統のみ）
                }

                TimeSpan delay = ReconnectPolicy.NextDelay(attempt++, _config.Reconnect, _jitter());
                LogReconnect(source, delay, attempt);
                _renderer.Reset(source); // 進行中 partial を破棄（FR-A1-06）

                try
                {
                    await Task.Delay(delay, ct).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    return; // 待機中の ct 停止（BR-RECONN-02）
                }
            }
        }
    }

    /// <summary>1 セッション（1 接続）を実行する。1件でも字幕を受信したら <paramref name="onCaption"/> を呼ぶ。</summary>
    private async Task RunOneSessionAsync(
        StreamSource source,
        IReadOnlyList<AudioDevice> devices,
        Action onCaption,
        CancellationToken ct)
    {
        IAsyncEnumerable<AudioFrame> frames = OpenFrames(source, devices, ct);
        var options = StreamingOptions.ForSource(source, _config.Language, _config.SampleRate);

        await foreach (LiveCaption caption in
                       _transcribe.StreamCaptionsAsync(frames, options, ct).ConfigureAwait(false))
        {
            onCaption();
            _renderer.Render(caption, source);
            if (!caption.IsPartial)
            {
                _sink?.Append(caption, source); // 確定字幕のみ永続化（FR-B1F-01。partial は渡さない）
            }
        }
    }

    private IAsyncEnumerable<AudioFrame> OpenFrames(
        StreamSource source,
        IReadOnlyList<AudioDevice> devices,
        CancellationToken ct)
    {
        if (source == StreamSource.Self)
        {
            AudioDevice mic = ResolveDefault(devices, DeviceDirection.Capture, "マイク(self)");
            return _capture.CaptureMicrophone(mic, ct);
        }

        AudioDevice render = ResolveDefault(devices, DeviceDirection.Render, "再生(others ループバック)");
        return _capture.CaptureLoopback(render, ct);
    }

    private static AudioDevice ResolveDefault(
        IReadOnlyList<AudioDevice> devices,
        DeviceDirection direction,
        string label)
    {
        AudioDevice? device = devices.FirstOrDefault(d => d.Direction == direction && d.IsDefault)
            ?? devices.FirstOrDefault(d => d.Direction == direction);
        return device ?? throw new InvalidOperationException($"{label} デバイスが見つかりません。");
    }

    // --- ログ（FR-A1-08）。字幕テキスト・認証情報・例外メッセージは載せない（NFR-SEC-04 / BR-ERR-04）。 ---

    private static void LogReconnect(StreamSource source, TimeSpan delay, int attempt) =>
        Console.Error.WriteLine(
            $"[再接続] 系統={source.Label()} 試行={attempt} 待機={delay.TotalMilliseconds:F0}ms");

    private static void LogStop(StreamSource source, bool permanent, string reason) =>
        Console.Error.WriteLine(
            $"[系統停止] 系統={source.Label()} 恒久={permanent} 理由={reason}");

    // 任意診断（既定OFF）。SUBTEXT_DIAG_RECONNECT=1 のとき、再接続/停止の原因例外を
    // 分類・型名・メッセージ・InnerException 連鎖で stderr に出す。字幕本文は含まず API/接続エラー文のみ
    // （BR-ERR-04）。全セッション即失敗の真因切り分け用で、本番（既定）挙動は不変。
    private static void DiagnoseFault(StreamSource source, Exception ex)
    {
        if (Environment.GetEnvironmentVariable("SUBTEXT_DIAG_RECONNECT") != "1")
        {
            return;
        }

        Console.Error.WriteLine(
            $"[診断] 系統={source.Label()} 分類={ReconnectPolicy.Classify(ex)} {ex.GetType().Name}: {ex.Message}");
        for (Exception? inner = ex.InnerException; inner is not null; inner = inner.InnerException)
        {
            Console.Error.WriteLine($"[診断]   原因: {inner.GetType().Name}: {inner.Message}");
        }
    }
}
