using Subtext.Capture;
using Subtext.Recorder;

// Unit A CLI エントリ（L2 準備 / L6 停止 / L8 異常）。
// 終了コード: 0=正常, 1=録音中の致命的異常(部分保存済), 2=起動時/その他の異常。
try
{
    RecorderConfig config = RecorderConfigLoader.Load(args);

    var capture = new WasapiAudioCapture();
    IReadOnlyList<AudioDevice> devices = capture.ListDevices();

    var resolver = new DeviceResolver();
    (AudioDevice self, AudioDevice others) = resolver.Resolve(devices, config.InputDevice, config.OutputDevice);

    // BR-DEV-04: 採用デバイスを表示（混入確認の起点）。
    Console.WriteLine($"[self/mic]    {self.Name}{(self.IsDefault ? " (default)" : string.Empty)}");
    Console.WriteLine($"[others/loop] {others.Name}{(others.IsDefault ? " (default)" : string.Empty)}");

    // L2: セッション準備。sessionId はローカル時刻ベース（Q6=A, BR-IO-01）。書式固定の理由は SessionId 参照。
    string sessionId = SessionId.FromLocalTime(DateTime.Now);
    var options = new RecorderOptions(
        config.OutputDir,
        sessionId,
        TimeSpan.FromMinutes(config.MaxDurationMinutes),
        self,
        others);

    // L6: 停止条件 = 手動(Ctrl+C) / 上限到達(Q1=B) / stop-file 出現(H2)。
    using var cts = new CancellationTokenSource(options.MaxDuration);
    Console.CancelKeyPress += (_, e) =>
    {
        e.Cancel = true; // プロセス即殺を防ぎ、フラッシュの猶予を得る
        try
        {
            cts.Cancel();
        }
        catch (ObjectDisposedException)
        {
            // 録音完了後（cts 破棄後）の Ctrl+C。停止対象が無いため無視する。
        }
    };

    // H2: stop-file による graceful 停止。シェル経由の SIGINT はネイティブコンソールプロセスへ
    // CTRL_C_EVENT を伴わず即殺(TerminateProcess 相当)になり finally/flush が走らないため、
    // コンソールシグナルに依存しない停止手段を提供する。出現で Ctrl+C と同じく cts を畳む。
    string? stopFile = string.IsNullOrWhiteSpace(config.StopFile) ? null : config.StopFile;
    if (stopFile is not null)
    {
        if (File.Exists(stopFile)) File.Delete(stopFile); // stale 除去（失敗時は起動時例外で顕在化=fail-fast）
        _ = Task.Run(async () =>
        {
            while (!cts.IsCancellationRequested)
            {
                if (File.Exists(stopFile)) { cts.Cancel(); break; }
                try { await Task.Delay(250, cts.Token); }
                catch (OperationCanceledException) { break; }
            }
        });
    }

    // 起動情報。オーケストレータ(Claude 等)が sessionId / stop-file を把握できるよう機械可読行も出す。
    Console.WriteLine($"sessionId={sessionId}");
    if (stopFile is not null) Console.WriteLine($"stopFile={stopFile}");
    Console.WriteLine(
        $"Recording session {sessionId}. Press Ctrl+C{(stopFile is not null ? " or create stop-file" : string.Empty)} to stop (auto-stop at {config.MaxDurationMinutes} min).");

    // 品質警告（BR-QLT-03）は stderr へ。字幕・PII は含まない。
    var recorder = new SyncRecorder(new AudioNormalizer(), Console.Error.WriteLine);
    IAsyncEnumerable<AudioFrame> selfStream = capture.CaptureMicrophone(self, cts.Token);
    IAsyncEnumerable<AudioFrame> othersStream = capture.CaptureLoopback(others, cts.Token);

    RecordingManifest manifest =
        await recorder.RecordAsync(selfStream, othersStream, options, cts.Token);

    string outDir = Path.Combine(options.OutputDir, sessionId);
    Console.WriteLine($"Saved to {outDir} (status={manifest.Status}).");
    return 0;
}
catch (RecordingException ex)
{
    // L8: 録音中の致命的異常。部分データは保存済み（manifest=incomplete）。
    Console.Error.WriteLine($"ERROR: {ex.Message}");
    return 1;
}
catch (Exception ex)
{
    // 起動時(デバイス不在/設定不整合)などの異常。BR-ERR-04: 秘密情報は出さない。
    Console.Error.WriteLine($"FATAL: {ex.Message}");
    return 2;
}
