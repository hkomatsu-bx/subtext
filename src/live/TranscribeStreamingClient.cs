using System.Runtime.CompilerServices;
using System.Threading.Channels;
using Amazon;
using Amazon.TranscribeStreaming;
using Amazon.TranscribeStreaming.Model;
using Subtext.Capture;

namespace Subtext.Live;

/// <summary>
/// <see cref="ILiveTranscribeClient"/> の AWS 実装（L2/L3 / BR-STREAM）。
/// AWSSDK.TranscribeStreaming v4 の <c>StartStreamTranscription</c> を用い、native フレームを
/// 16kHz/mono/16bit PCM（<see cref="LivePcmConverter"/>）へ変換して送信、partial/final を字幕化する。
///
/// 認証情報はコードに持たず、AWS 既定の資格情報解決（プロファイル/SSO/環境変数）に委ねる（NFR-SEC-07）。
/// VAD は持たず無音分割は Transcribe 側に委ねる（NFR-06）。相手系統も話者分離は無効（Q4=A, ShowSpeakerLabel=false）。
/// </summary>
public sealed class TranscribeStreamingClient(LiveSttConfig config, LivePcmConverter converter)
    : ILiveTranscribeClient
{
    // capture 消費タスクが PCM を貯める中間バッファの上限（約2秒分 = 20ms フレーム × 100）。
    // bounded + Wait で「SDK pull 速度 ≒ capture 消費速度」の背圧を保ち、SDK 遅延時の
    // メモリ膨張を防ぐ（Unbounded は背圧が消えるため不採用）。
    private const int PcmChannelCapacity = 100;

    private readonly LiveSttConfig _config = config ?? throw new ArgumentNullException(nameof(config));
    private readonly LivePcmConverter _converter = converter ?? throw new ArgumentNullException(nameof(converter));

    public async IAsyncEnumerable<LiveCaption> StreamCaptionsAsync(
        IAsyncEnumerable<AudioFrame> frames,
        StreamingOptions options,
        [EnumeratorCancellation] CancellationToken ct)
    {
        ArgumentNullException.ThrowIfNull(frames);
        ArgumentNullException.ThrowIfNull(options);

        // 送信した音声時間 ↔ 壁時計の対応。Transcribe の StartTime は音声時間基準で、keep-alive の
        // 無音注入があると壁時計と大きくずれるため、開始時刻からの単純加算では captureUtc を作れない。
        var timeline = new AudioTimeline(options.SampleRate);

        using var client = new AmazonTranscribeStreamingClient(RegionEndpoint.GetBySystemName(_config.AwsRegion));

        // capture の async-enumerator を SDK publisher へ直結させない（NotSupportedException 回避）。
        // SDK は HTTP/2 送信スレッドから publisher を並行に pull / teardown するため、async iterator を
        // 直接 MoveNext/Dispose すると「MoveNext 保留中の Dispose」で例外が出る（Self/Others 2系統並行時のみ顕在化）。
        // 対策: 単一の専用タスク（PumpFramesAsync）で capture を消費して PCM を Channel へ書き、
        // publisher は Channel から読むだけにする。capture 列挙子に触れるのは pump タスクのみ。
        var pcmChannel = Channel.CreateBounded<byte[]>(new BoundedChannelOptions(PcmChannelCapacity)
        {
            FullMode = BoundedChannelFullMode.Wait,
            SingleReader = false, // SDK が複数の送信スレッドから pull しうる
            SingleWriter = true,  // producer は単一の pump タスクのみ
        });

        // teardown（字幕読み取り終了/キャンセル）で pump を確実に止めるためのリンク CTS。
        // token は PumpFramesAsync 内で処理するため Task.Run には渡さない（await producer が
        // 起動前キャンセルで TaskCanceledException を投げるのを避ける）。
        using var producerCts = CancellationTokenSource.CreateLinkedTokenSource(ct);

        // FR-A1-05: capture フレームが keep-alive 間隔だけ途絶したら 20ms 無音 PCM を注入し、
        // Transcribe の 15s idle timeout を起こさない。間隔 0 以下で無効（従来の単純 pump）。
        int keepAliveMs = _config.Reconnect.KeepAliveIntervalMs;
        byte[]? silentChunk = keepAliveMs > 0 ? new byte[options.SampleRate / 50 * 2] : null; // 20ms = 1/50s, 16bit mono
        Func<CancellationToken, Task>? keepAliveWait =
            keepAliveMs > 0 ? token => Task.Delay(keepAliveMs, token) : null;

        // リサンプルの連続性状態は接続（セッション）ごとに独立させる。状態を触るのは
        // 単一の pump タスクのみ＝競合しない（PumpFramesAsync の不変条件）。
        ResampleState resampleState = ResampleState.Initial;
        Func<AudioFrame, byte[]> toPcm = frame =>
        {
            (byte[] pcm, resampleState) = _converter.ToPcm16Mono16k(frame, resampleState);
            return pcm;
        };

        Task producer = Task.Run(
            () => PumpFramesAsync(
                frames, toPcm, pcmChannel.Writer, timeline, producerCts.Token,
                silentChunk, keepAliveWait));

        // v4 の AudioStreamPublisher は「次イベントを返す pull 関数」（null で終端）。
        // Channel から PCM を1チャンク取り出して AudioEvent を返すだけ＝capture 列挙子には触れない。
        async Task<IAudioStreamEvent> NextAudioEventAsync()
        {
            try
            {
                byte[] pcm = await pcmChannel.Reader.ReadAsync(ct).ConfigureAwait(false);
                return new AudioEvent { AudioChunk = new MemoryStream(pcm) };
            }
            catch (OperationCanceledException)
            {
                return null!; // キャンセル時はストリームを正常終端させる（teardown）
            }
            catch (ChannelClosedException ex) when (ex.InnerException is null)
            {
                return null!; // capture 正常終端。SDK はこれでストリームを閉じる。
            }
            // capture 例外で完了した場合は ChannelClosedException(inner=capture例外) がそのまま伝播し fail-fast。
        }

        StartStreamTranscriptionRequest request =
            BuildStartRequest(options, _config.VocabularyName, NextAudioEventAsync);

        StartStreamTranscriptionResponse response;
        try
        {
            response = await client.StartStreamTranscriptionAsync(request, ct).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            // 起動失敗は fail-fast（BR-ERR-01）。メッセージに字幕/認証情報は載せない（BR-ERR-04）。
            producerCts.Cancel();
            await producer.ConfigureAwait(false); // pump を回収してからスロー
            throw new InvalidOperationException(
                $"Transcribe Streaming の開始に失敗しました（系統={options.Source.Label()}）。", ex);
        }

        try
        {
            await foreach (LiveCaption caption in ReadCaptionsAsync(response, options, timeline, ct)
                               .ConfigureAwait(false))
            {
                yield return caption;
            }
        }
        finally
        {
            // 字幕読み取り終了/中断時に pump を確実に停止・回収する（リーク防止・BR-ERR-01）。
            // capture 例外は Channel 経由で既に NextAudioEventAsync→SDK に伝播済み。pump は全例外を
            // writer 完了へ変換するため faulted にならず、await producer はスローしない。
            producerCts.Cancel();
            await producer.ConfigureAwait(false);
        }
    }

    /// <summary>
    /// <see cref="StartStreamTranscriptionRequest"/> を組み立てる純粋ビルダー（FR-C1-01・DG-C1-3）。
    /// AWS を呼ばず request オブジェクトを返すのみ＝実 AWS / WASAPI 非依存で単体テスト可能。
    /// <paramref name="vocabularyName"/> が指定（非 null/非空白）された場合のみカスタム語彙を付与する
    /// （未設定なら従来動作＝語彙なし, BR-VOCAB-02）。
    /// </summary>
    internal static StartStreamTranscriptionRequest BuildStartRequest(
        StreamingOptions options,
        string? vocabularyName,
        Func<Task<IAudioStreamEvent>> audioStreamPublisher)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(audioStreamPublisher);

        var request = new StartStreamTranscriptionRequest
        {
            LanguageCode = options.LanguageCode,
            MediaSampleRateHertz = options.SampleRate,
            MediaEncoding = MediaEncoding.Pcm,
            ShowSpeakerLabel = false, // Q4=A: 系統ラベルのみ（相手内分離なし）
            AudioStreamPublisher = audioStreamPublisher,
        };

        if (!string.IsNullOrWhiteSpace(vocabularyName))
        {
            request.VocabularyName = vocabularyName; // FR-C1-01
        }

        return request;
    }

    /// <summary>
    /// capture フレームを単一タスクで消費し PCM へ変換して <paramref name="writer"/> へ書く producer。
    /// async-enumerator に触れるのは本タスクのみ＝SDK publisher の並行 pull/teardown と構造的に分離する。
    /// 送信したチャンクは <see cref="AudioTimeline"/> へ記録する（captureUtc 算出の基準）。
    /// 空 PCM はスキップ（従来動作）。正常完了/キャンセルは
    /// writer を完了し、capture 例外は writer をエラー完了して Channel 経由で伝播する（fail-fast, BR-ERR-01）。
    /// </summary>
    internal static async Task PumpFramesAsync(
        IAsyncEnumerable<AudioFrame> frames,
        Func<AudioFrame, byte[]> toPcm,
        ChannelWriter<byte[]> writer,
        AudioTimeline timeline,
        CancellationToken ct,
        byte[]? keepAliveSilentChunk = null,
        Func<CancellationToken, Task>? keepAliveWait = null)
    {
        try
        {
            // keep-alive 無効時は従来の単純消費。enumerator に触れるのは本タスクのみ（不変条件）。
            if (keepAliveSilentChunk is null || keepAliveWait is null)
            {
                await PumpSimpleAsync(frames, toPcm, writer, timeline, ct).ConfigureAwait(false);
            }
            else
            {
                await PumpWithKeepAliveAsync(
                    frames, toPcm, writer, timeline, keepAliveSilentChunk, keepAliveWait, ct).ConfigureAwait(false);
            }

            writer.TryComplete(); // capture 正常終端
        }
        catch (OperationCanceledException)
        {
            writer.TryComplete(); // teardown による正常停止（例外扱いしない）
        }
        catch (Exception ex)
        {
            writer.TryComplete(ex); // capture 例外を Channel 経由で伝播（fail-fast）
        }
    }

    private static async Task PumpSimpleAsync(
        IAsyncEnumerable<AudioFrame> frames,
        Func<AudioFrame, byte[]> toPcm,
        ChannelWriter<byte[]> writer,
        AudioTimeline timeline,
        CancellationToken ct)
    {
        await foreach (AudioFrame frame in frames.WithCancellation(ct).ConfigureAwait(false))
        {
            byte[] pcm = toPcm(frame);
            if (pcm.Length > 0)
            {
                timeline.Advance(pcm.Length, frame.CaptureUtc); // 音声時間 ↔ 壁時計の基準を更新
                await writer.WriteAsync(pcm, ct).ConfigureAwait(false);
            }
        }
    }

    /// <summary>
    /// 「次フレーム待ち」と「keep-alive 間隔」をレースさせ、間隔が先に経過したら無音 PCM を注入する
    /// （FR-A1-05）。<see cref="IAsyncEnumerator{T}.MoveNextAsync"/> は単一の保留 Task として保持し、
    /// 完了したものだけを再発行する＝過去の NotSupportedException（並行 MoveNext/Dispose）を再来させない。
    /// enumerator に触れるのは本タスクのみ。
    /// </summary>
    private static async Task PumpWithKeepAliveAsync(
        IAsyncEnumerable<AudioFrame> frames,
        Func<AudioFrame, byte[]> toPcm,
        ChannelWriter<byte[]> writer,
        AudioTimeline timeline,
        byte[] silentChunk,
        Func<CancellationToken, Task> keepAliveWait,
        CancellationToken ct)
    {
        // await using は使わない。保留中の MoveNextAsync を持ったまま DisposeAsync すると
        // 「MoveNext 保留中の Dispose」でハング/NotSupportedException を起こす（過去のバグの再来）。
        // 破棄前に必ず保留 moveTask を観測してから DisposeAsync する（下の finally）。
        IAsyncEnumerator<AudioFrame> e = frames.GetAsyncEnumerator(ct);
        Task<bool> moveTask = e.MoveNextAsync().AsTask(); // 保留中の MoveNext は単一インスタンスのみ
        try
        {
            while (true)
            {
                ct.ThrowIfCancellationRequested();
                using var waitCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                Task waitTask = keepAliveWait(waitCts.Token);

                Task done = await Task.WhenAny(moveTask, waitTask).ConfigureAwait(false);
                if (done == moveTask)
                {
                    waitCts.Cancel(); // 保留中の keep-alive タイマを破棄（タイマ滞留を防ぐ）
                    bool has = await moveTask.ConfigureAwait(false);
                    if (!has)
                    {
                        break; // capture 正常終端
                    }

                    AudioFrame frame = e.Current;
                    byte[] pcm = toPcm(frame);
                    if (pcm.Length > 0)
                    {
                        timeline.Advance(pcm.Length, frame.CaptureUtc);
                        await writer.WriteAsync(pcm, ct).ConfigureAwait(false);
                    }

                    moveTask = e.MoveNextAsync().AsTask(); // 次フレーム待ちを再発行
                }
                else
                {
                    // フレーム途絶＝idle timeout 回避のため無音を注入。moveTask は保留のまま次ラウンドへ。
                    // 無音も音声時間を進めるため、壁時計の基準を注入時刻へ寄せ直す（ずれの累積を断つ）。
                    timeline.Advance(silentChunk.Length, DateTime.UtcNow);
                    await writer.WriteAsync(silentChunk, ct).ConfigureAwait(false);
                }
            }
        }
        finally
        {
            // 破棄前に保留中の MoveNext を必ず観測する（並行 MoveNext/Dispose を避ける）。
            // ct キャンセルで capture 列挙子の MoveNextAsync は完了する（OCE もしくは終端）。
            try
            {
                await moveTask.ConfigureAwait(false);
            }
            catch
            {
                // 観測のみ。capture 例外は外側 PumpFramesAsync の catch が Channel 経由で扱う。
            }

            await e.DisposeAsync().ConfigureAwait(false);
        }
    }

    private static async IAsyncEnumerable<LiveCaption> ReadCaptionsAsync(
        StartStreamTranscriptionResponse response,
        StreamingOptions options,
        AudioTimeline timeline,
        [EnumeratorCancellation] CancellationToken ct)
    {
        // イベントストリームを Channel へブリッジし IAsyncEnumerable として返す。
        var channel = Channel.CreateUnbounded<LiveCaption>();

        response.TranscriptResultStream.TranscriptEventReceived += (_, args) =>
        {
            foreach (Result result in args.EventStreamEvent.Transcript.Results)
            {
                if (result.Alternatives is null || result.Alternatives.Count == 0)
                {
                    continue;
                }

                // v4 は数値/真偽が nullable。欠落時は 0 / partial 扱いに倒す。
                var offset = TimeSpan.FromSeconds(result.StartTime ?? 0);
                // offset は「送信済み音声の累積時間」基準。壁時計へは AudioTimeline 経由で写す
                // （開始時刻への単純加算は keep-alive 無音のぶんだけ過去にずれる）。
                DateTime? captureUtc = timeline.ToWallClock(offset);
                channel.Writer.TryWrite(new LiveCaption(
                    Speaker: options.Source.Label(),
                    Text: result.Alternatives[0].Transcript ?? string.Empty,
                    IsPartial: result.IsPartial ?? true,
                    Offset: offset,
                    CaptureUtc: captureUtc)); // 遅延は受信時刻基準で表示層が確定（Q3=A）
            }
        };

        response.TranscriptResultStream.ExceptionReceived += (_, args) =>
            channel.Writer.TryComplete(
                new InvalidOperationException("Transcribe Streaming でエラーを受信しました。", args.EventStreamException));

        // バックグラウンドでイベント処理を駆動し、完了したら channel を閉じる。
        Task processing = Task.Run(async () =>
        {
            try
            {
                await response.TranscriptResultStream.StartProcessingAsync().ConfigureAwait(false);
                channel.Writer.TryComplete();
            }
            catch (Exception ex)
            {
                channel.Writer.TryComplete(ex);
            }
        }, ct);

        try
        {
            await foreach (LiveCaption caption in channel.Reader.ReadAllAsync(ct).ConfigureAwait(false))
            {
                yield return caption;
            }
        }
        finally
        {
            // イベントストリームと読み取りタスクを必ず回収する。放置すると HTTP/2 ストリームと
            // タスクが残り、**再接続のたびに蓄積する**（Unit C は切断のたびに新しいセッションを張る）。
            // 先に Dispose して StartProcessingAsync を解き、そのうえでタスクを観測する。
            response.TranscriptResultStream.Dispose();
            try
            {
                await processing.ConfigureAwait(false);
            }
            catch (Exception)
            {
                // 観測のみ。異常は channel 経由で既に呼び出し側へ伝播している（teardown 時は無害）。
            }
        }
    }
}
