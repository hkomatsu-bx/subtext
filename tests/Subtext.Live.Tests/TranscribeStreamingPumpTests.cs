using System.Runtime.CompilerServices;
using System.Threading.Channels;
using Subtext.Capture;
using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

/// <summary>
/// <see cref="TranscribeStreamingClient.PumpFramesAsync"/> の純ロジック検証。
/// capture の async-enumerator を SDK publisher へ直結させない中間 Channel 方式の producer 部。
/// SDK・AWS 実機には触れず、合成 <see cref="IAsyncEnumerable{T}"/> と擬似 PCM 変換で挙動を固定する。
/// </summary>
public sealed class TranscribeStreamingPumpTests
{
    private const int SampleRate = 16_000;

    private static AudioFrame Frame(DateTime? captureUtc = null) =>
        new([], AudioFormat.Normalized, captureUtc ?? DateTime.UtcNow);

    private static async IAsyncEnumerable<AudioFrame> FramesAsync(
        IEnumerable<AudioFrame> items,
        [EnumeratorCancellation] CancellationToken ct = default)
    {
        foreach (AudioFrame frame in items)
        {
            ct.ThrowIfCancellationRequested();
            yield return frame;
            await Task.Yield();
        }
    }

    // 1件目を流した後、gate が完了（または ct キャンセル）するまで次フレームの取得を保留する。
    // pump を capture の MoveNextAsync 待ちで決定的に停止させ、teardown キャンセルを再現するため。
    private static async IAsyncEnumerable<AudioFrame> GatedFramesAsync(
        Task gate,
        [EnumeratorCancellation] CancellationToken ct = default)
    {
        yield return Frame();
        await gate.WaitAsync(ct).ConfigureAwait(false); // 解放/キャンセルまで MoveNext を保留
        yield return Frame(); // キャンセル時は到達しない
    }

    private static async IAsyncEnumerable<AudioFrame> ThrowAfterFirstAsync()
    {
        yield return Frame();
        await Task.Yield();
        throw new InvalidOperationException("capture boom");
    }

    [Fact]
    public async Task PumpFramesAsync_WritesConvertedPcmInOrder_ThenCompletes()
    {
        // Arrange: 3 フレーム → 変換結果を順に [1],[2],[3]。
        var frames = new[] { Frame(), Frame(), Frame() };
        int seq = 0;
        Func<AudioFrame, byte[]> toPcm = _ => new[] { (byte)(++seq) };
        var channel = Channel.CreateBounded<byte[]>(10);
        var timeline = new AudioTimeline(SampleRate);

        // Act
        await TranscribeStreamingClient.PumpFramesAsync(
            FramesAsync(frames), toPcm, channel.Writer, timeline, CancellationToken.None);

        // Assert: 入力順を保って書き込まれ、正常終端する。
        var got = new List<byte>();
        await foreach (byte[] pcm in channel.Reader.ReadAllAsync())
        {
            got.Add(pcm[0]);
        }

        Assert.Equal(new byte[] { 1, 2, 3 }, got);
    }

    [Fact]
    public async Task PumpFramesAsync_SkipsEmptyPcm()
    {
        // Arrange: 中央フレームの変換結果を空にする。
        var frames = new[] { Frame(), Frame(), Frame() };
        Func<AudioFrame, byte[]> toPcm = f =>
            ReferenceEquals(f, frames[1]) ? Array.Empty<byte>() : new byte[] { 9 };
        var channel = Channel.CreateBounded<byte[]>(10);
        var timeline = new AudioTimeline(SampleRate);

        // Act
        await TranscribeStreamingClient.PumpFramesAsync(
            FramesAsync(frames), toPcm, channel.Writer, timeline, CancellationToken.None);

        // Assert: 空はスキップされ 2 件のみ。
        var count = 0;
        await foreach (byte[] _ in channel.Reader.ReadAllAsync())
        {
            count++;
        }

        Assert.Equal(2, count);
    }

    [Fact]
    public async Task PumpFramesAsync_MapsAudioOffsetToFrameCaptureTime()
    {
        // Arrange: 1 フレーム = 1 秒ぶんの PCM。既知の capture 時刻を持たせる。
        var first = new DateTime(2026, 6, 24, 1, 2, 3, DateTimeKind.Utc);
        var frames = new[] { Frame(first), Frame(first.AddSeconds(1)) };
        var channel = Channel.CreateBounded<byte[]>(10);
        var timeline = new AudioTimeline(SampleRate);
        var oneSecond = new byte[SampleRate * 2]; // 16bit mono

        // Act
        await TranscribeStreamingClient.PumpFramesAsync(
            FramesAsync(frames), _ => oneSecond, channel.Writer, timeline, CancellationToken.None);

        // Assert: 音声時間のオフセットがフレームの捕捉時刻へ写る（Transcribe の StartTime → captureUtc）。
        Assert.Equal(first.AddSeconds(1), timeline.ToWallClock(TimeSpan.FromSeconds(1)));
        Assert.Equal(first, timeline.ToWallClock(TimeSpan.Zero));
    }

    [Fact]
    public async Task PumpFramesAsync_OnCancellation_CompletesWithoutError()
    {
        // Arrange: 1件目を書いた後、capture が次フレーム取得で保留（gate 待ち）した状態にする。
        var channel = Channel.CreateBounded<byte[]>(10);
        var timeline = new AudioTimeline(SampleRate);
        var gate = new TaskCompletionSource(); // 完了させない＝MoveNext を保留させる
        using var cts = new CancellationTokenSource();

        Task pump = TranscribeStreamingClient.PumpFramesAsync(
            GatedFramesAsync(gate.Task, cts.Token), _ => new byte[] { 1 }, channel.Writer, timeline, cts.Token);

        // 1件目が読めた＝pump は frame1 を書き終え、次フレーム取得(MoveNext)で gate 待ちに入っている。
        byte[] first = await channel.Reader.ReadAsync();
        Assert.Equal(new byte[] { 1 }, first);

        // Act: teardown 相当のキャンセル。MoveNext 待ちが OperationCanceledException で解ける。
        cts.Cancel();
        await pump.WaitAsync(TimeSpan.FromSeconds(5)); // ハングなら TimeoutException で顕在化

        // Assert: writer はエラーなしで完了（teardown 正常終了）。
        await channel.Reader.Completion;
        Assert.True(channel.Reader.Completion.IsCompletedSuccessfully);
    }

    [Fact]
    public async Task PumpFramesAsync_OnCaptureFailure_PropagatesViaChannelClosedException()
    {
        // Arrange: 1 件目の後に capture が例外を投げる。
        var channel = Channel.CreateBounded<byte[]>(10);
        var timeline = new AudioTimeline(SampleRate);

        // Act: pump 自体は全例外を握って writer をエラー完了するためスローしない。
        await TranscribeStreamingClient.PumpFramesAsync(
            ThrowAfterFirstAsync(), _ => new byte[] { 1 }, channel.Writer, timeline, CancellationToken.None);

        // Assert: 本番と同じ ReadAsync 経路では、1 件読めた後に capture 例外を内包する
        // ChannelClosedException で終端する（NextAudioEventAsync が fail-fast でこれを SDK へ伝播）。
        byte[] firstRead = await channel.Reader.ReadAsync();
        Assert.Equal(new byte[] { 1 }, firstRead);

        var closed = await Assert.ThrowsAsync<ChannelClosedException>(async () =>
        {
            while (true)
            {
                await channel.Reader.ReadAsync();
            }
        });
        Assert.IsType<InvalidOperationException>(closed.InnerException);
    }

    // ct キャンセルまでフレームを出さない（idle 状態を再現）。キャンセルは正常終端に倒す。
    private static async IAsyncEnumerable<AudioFrame> IdleUntilCancelAsync(
        [EnumeratorCancellation] CancellationToken ct)
    {
        try
        {
            await Task.Delay(Timeout.Infinite, ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            // 正常終端させ、保留 MoveNext を faulted にしない。
        }

        yield break;
    }

    [Fact]
    public async Task PumpFramesAsync_InjectsSilence_WhenFramesStall()
    {
        // Arrange: フレームが途絶（idle）した状態。最初の3回の keep-alive 間隔は即経過させ無音を3回注入、
        // 以降はキャンセルまで待機する（密ループを避け、pump を確実に停止可能にする）（FR-A1-05）。
        var silent = new byte[] { 0, 0 };
        var channel = Channel.CreateBounded<byte[]>(10);
        var timeline = new AudioTimeline(SampleRate);
        using var cts = new CancellationTokenSource();

        int waits = 0;
        Func<CancellationToken, Task> keepAliveWait = token =>
            System.Threading.Interlocked.Increment(ref waits) <= 3
                ? Task.CompletedTask                       // 間隔即経過 → 無音注入
                : Task.Delay(Timeout.Infinite, token);     // 以降はキャンセルまで待機

        Task pump = TranscribeStreamingClient.PumpFramesAsync(
            IdleUntilCancelAsync(cts.Token), _ => new byte[] { 9 }, channel.Writer, timeline, cts.Token,
            keepAliveSilentChunk: silent, keepAliveWait: keepAliveWait);

        // Assert: フレーム途絶中は無音 PCM が連続注入される。
        for (int i = 0; i < 3; i++)
        {
            byte[] chunk = await channel.Reader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(5));
            Assert.Equal(silent, chunk);
        }

        // teardown 相当のキャンセルで pump は正常終了する。
        cts.Cancel();
        await pump.WaitAsync(TimeSpan.FromSeconds(5));
        await channel.Reader.Completion;
        Assert.True(channel.Reader.Completion.IsCompletedSuccessfully);
    }
}
