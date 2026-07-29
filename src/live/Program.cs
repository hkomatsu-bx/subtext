using Subtext.Capture;
using Subtext.Live;

// Subtext Unit C — ライブ字幕 CLI（FR-12/13/14）。
// 終了コード: 0=正常終了（Ctrl+C 含む）, 1=実行中の致命的異常, 2=起動時/設定の異常。
// 字幕テキスト・認証情報はログ/ファイルに残さない（NFR-SEC-04）。非永続（コンソールのみ）。

try
{
    LiveSttConfig config = LiveSttConfig.Load();

    var capture = new WasapiAudioCapture();
    var converter = new LivePcmConverter(config.SampleRate);
    var transcribe = new TranscribeStreamingClient(config, converter);
    var renderer = new CaptionRenderer();
    // B1F: CaptionSinkPath 設定時のみ確定字幕を JSONL 永続化する（未設定なら従来のコンソール専用）。
    // 所有権は本エントリ（using で flush/close 保証）。
    using ICaptionSink? sink = config.CaptionSinkPath is { } sinkPath
        ? new JsonlCaptionSink(sinkPath)
        : null;
    var app = new LiveSttApp(capture, transcribe, renderer, config, sink: sink);

    using var cts = new CancellationTokenSource();
    Console.CancelKeyPress += (_, e) =>
    {
        e.Cancel = true; // プロセス即時終了を抑止し協調キャンセルへ（Q5=A）
        try
        {
            cts.Cancel();
        }
        catch (ObjectDisposedException)
        {
            // 正常終了後（cts 破棄後）の Ctrl+C。停止対象が無いため無視する。
        }
    };

    // B1F: StopFilePath 設定時は .stop を検知して協調停止する（FR-B1F-03。ハーネス live 段が置く）。
    if (config.StopFilePath is { } stopFilePath)
    {
        StartStopFileWatcher(stopFilePath, cts);
    }

    Console.Out.WriteLine($"ライブ字幕を開始します（系統: {string.Join(", ", config.Sources)}）。Ctrl+C で停止。");

    try
    {
        await app.RunAsync(cts.Token);
        return 0;
    }
    catch (OperationCanceledException)
    {
        return 0; // Ctrl+C による正常終了
    }
    catch (Exception ex)
    {
        // 実行中の致命的異常（握りつぶさず安全側停止, BR-ERR-01）。メッセージに字幕/認証情報は含めない。
        // 原因（InnerException 連鎖）は型名＋メッセージのみ出力する（診断用・BR-ERR-04）。
        // スタックトレース/パスは出さない（漏洩回避）。
        Console.Error.WriteLine($"エラー: {ex.Message}");
        WriteCauseChain(ex);
        return 1;
    }
}
catch (Exception ex)
{
    // 起動時/設定の異常（BR-ERR-01）。
    Console.Error.WriteLine($"起動エラー: {ex.Message}");
    WriteCauseChain(ex);
    return 2;
}

// B1F: stop-file（`.stop`）を一定間隔でポーリングし、出現したら協調キャンセルする（FR-B1F-03）。
// ファイル存在の検知のみで本文・認証情報は扱わない。cts 破棄後（正常終了後）は無視する。
static void StartStopFileWatcher(string stopFilePath, CancellationTokenSource cts)
{
    _ = Task.Run(async () =>
    {
        try
        {
            while (!cts.IsCancellationRequested)
            {
                if (File.Exists(stopFilePath))
                {
                    cts.Cancel();
                    return;
                }

                await Task.Delay(TimeSpan.FromMilliseconds(250), cts.Token).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
        {
            // 停止確定（cts キャンセル）。正常終了。
        }
        catch (ObjectDisposedException)
        {
            // 正常終了後の cts 破棄。停止対象が無いため無視する。
        }
    });
}

// 例外の InnerException 連鎖を「型名: メッセージ」で出力する（診断補助, BR-ERR-04）。
// 環境変数 SUBTEXT_DIAG_STACK=1 のときのみ完全スタックを併記する（NotSupportedException 等の
// 発生箇所特定用。字幕到達前の起動失敗にのみ使う一時診断＝本文 PII を含まない）。
static void WriteCauseChain(Exception ex)
{
    for (Exception? inner = ex.InnerException; inner is not null; inner = inner.InnerException)
    {
        Console.Error.WriteLine($"  原因: {inner.GetType().Name}: {inner.Message}");
    }

    if (Environment.GetEnvironmentVariable("SUBTEXT_DIAG_STACK") == "1")
    {
        Console.Error.WriteLine("  --- 診断スタック（SUBTEXT_DIAG_STACK=1）---");
        Console.Error.WriteLine(ex.ToString());
    }
}
