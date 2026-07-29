using System.Net.Http;
using System.Runtime.CompilerServices;
using System.Text;
using Subtext.Capture;
using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

public sealed class LiveSttAppTests
{
    private static LiveSttConfig Config(params StreamSource[] sources) =>
        new("ap-northeast-1", "ja-JP", sources, 16000);

    [Fact]
    public async Task RunAsync_StreamsAllConfiguredSources()
    {
        // Arrange
        var output = new StringWriter(new StringBuilder());
        var renderer = new CaptionRenderer(output, now: () => DateTime.UtcNow);
        var app = new LiveSttApp(new FakeCapture(), new FakeTranscribe(), renderer, Config(StreamSource.Self, StreamSource.Others));

        // Act
        await app.RunAsync(CancellationToken.None);

        // Assert: 両系統の字幕が表示される（Q1=A）
        string text = output.ToString();
        Assert.Contains("自分", text);
        Assert.Contains("相手", text);
    }

    [Fact]
    public async Task RunAsync_OneSourceFatal_StopsOnlyThatSource_OtherContinues()
    {
        // D1: 旧 fail-fast 全停止を撤廃。fatal な系統だけ停止し、他系統は継続する。
        // FakeTranscribe は inner なしの InvalidOperationException を投げる＝ReconnectPolicy で Fatal 分類。
        var output = new StringWriter(new StringBuilder());
        var renderer = new CaptionRenderer(output);
        var transcribe = new FakeTranscribe(failOn: StreamSource.Others);
        var app = new LiveSttApp(new FakeCapture(), transcribe, renderer, Config(StreamSource.Self, StreamSource.Others));

        // 例外を投げずに完了する（自分系統は最後まで稼働）。
        await app.RunAsync(CancellationToken.None);

        string text = output.ToString();
        Assert.Contains("自分", text);     // 生存系統の字幕は出る
        Assert.DoesNotContain("相手]", text); // 相手系統は fatal 停止（字幕行なし。Reset も呼ばれない）
    }

    [Fact]
    public async Task RunAsync_Reconnects_OnTransient_ThenSucceeds()
    {
        // 一過性例外で2回失敗→3回目で成功。固定クロック＋ジッタ0で予算超過させず即時再接続。
        var output = new StringWriter(new StringBuilder());
        var renderer = new CaptionRenderer(output);
        var transcribe = new ScriptedTranscribe(attempt =>
            attempt < 2 ? new HttpRequestException("connection reset") : null);
        var fixedNow = new DateTime(2026, 6, 24, 0, 0, 0, DateTimeKind.Utc);
        var app = new LiveSttApp(
            new FakeCapture(), transcribe, renderer, Config(StreamSource.Self),
            clock: () => fixedNow, jitter: () => 0.0);

        await app.RunAsync(CancellationToken.None);

        Assert.Equal(3, transcribe.CallCount);          // 2回失敗＋成功
        Assert.Contains("テスト字幕", output.ToString()); // 最終的に字幕受信
    }

    [Fact]
    public async Task RunAsync_RefreshesDeviceListOnEachReconnect()
    {
        // デバイス一覧を起動時に1度だけ取ると、会議中に既定デバイスが切り替わった（イヤホンの着脱・
        // 会議アプリのデバイス変更）あとの再接続が消えたデバイスを開き続け、**エラーも出さずに
        // 字幕が止まる**。接続のたびに取り直すことを呼び出し回数で固定する。
        var capture = new FakeCapture();
        var renderer = new CaptionRenderer(new StringWriter());
        var transcribe = new ScriptedTranscribe(attempt =>
            attempt < 2 ? new HttpRequestException("connection reset") : null);
        var fixedNow = new DateTime(2026, 6, 24, 0, 0, 0, DateTimeKind.Utc);
        var app = new LiveSttApp(
            capture, transcribe, renderer, Config(StreamSource.Self),
            clock: () => fixedNow, jitter: () => 0.0);

        await app.RunAsync(CancellationToken.None);

        Assert.Equal(3, transcribe.CallCount); // 2回失敗＋成功
        Assert.Equal(3, capture.ListDevicesCalls); // 接続ごとに取り直す
    }

    [Fact]
    public async Task RunAsync_BudgetExhausted_StopsSource()
    {
        // 一過性例外が続き、再接続ウィンドウ（既定5分）超過で当該系統を恒久停止する（BR-RECONN-03）。
        // クロックを2分/呼で進め、3回目の予算判定（t0+6分）で超過→停止。
        var renderer = new CaptionRenderer(new StringWriter());
        var transcribe = new ScriptedTranscribe(_ => new HttpRequestException("connection reset"));
        var baseT = new DateTime(2026, 6, 24, 0, 0, 0, DateTimeKind.Utc);
        int tick = 0;
        var app = new LiveSttApp(
            new FakeCapture(), transcribe, renderer, Config(StreamSource.Self),
            clock: () => baseT.AddMinutes(2 * tick++), jitter: () => 0.0);

        // 例外を投げずに完了する（無限リトライしない）。
        await app.RunAsync(CancellationToken.None);

        Assert.Equal(3, transcribe.CallCount); // 3回試行後に予算超過で停止
    }

    [Fact]
    public async Task RunAsync_WritesOnlyFinalCaptionsToSink()
    {
        // B1F・FR-B1F-01: sink には確定 final のみ渡す（partial は渡さない）。
        // FakeTranscribe は系統ごとに partial 1件＋final 1件を出す。
        var renderer = new CaptionRenderer(new StringWriter());
        var sink = new RecordingSink();
        var app = new LiveSttApp(
            new FakeCapture(), new FakeTranscribe(), renderer, Config(StreamSource.Self), sink: sink);

        await app.RunAsync(CancellationToken.None);

        Assert.All(sink.Captions, entry => Assert.False(entry.Caption.IsPartial));
        Assert.Contains(sink.Captions, entry => entry.Caption.Text == "テスト字幕確定");
        Assert.DoesNotContain(sink.Captions, entry => entry.Caption.Text == "テスト字幕");
        Assert.Equal(StreamSource.Self, Assert.Single(sink.Captions).Source);
    }

    // --- フェイク実装（実 WASAPI / 実 AWS を呼ばない seam） ---

    private sealed class RecordingSink : ICaptionSink
    {
        public List<(LiveCaption Caption, StreamSource Source)> Captions { get; } = new();

        public void Append(LiveCaption caption, StreamSource source) => Captions.Add((caption, source));

        public void Dispose()
        {
        }
    }


    private sealed class FakeCapture : IAudioCapture
    {
        /// <summary>ListDevices の呼び出し回数（再接続ごとに取り直すことの検証用）。</summary>
        public int ListDevicesCalls { get; private set; }

        public IReadOnlyList<AudioDevice> ListDevices()
        {
            ListDevicesCalls++;
            return new[]
            {
                new AudioDevice("render-1", "Speakers", DeviceDirection.Render, IsDefault: true),
                new AudioDevice("capture-1", "Mic", DeviceDirection.Capture, IsDefault: true),
            };
        }

        public IAsyncEnumerable<AudioFrame> CaptureLoopback(AudioDevice outputDevice, CancellationToken ct)
            => Frames(ct);

        public IAsyncEnumerable<AudioFrame> CaptureMicrophone(AudioDevice inputDevice, CancellationToken ct)
            => Frames(ct);

        private static async IAsyncEnumerable<AudioFrame> Frames(
            [EnumeratorCancellation] CancellationToken ct)
        {
            for (int i = 0; i < 2 && !ct.IsCancellationRequested; i++)
            {
                yield return new AudioFrame(new byte[] { 0, 0 }, AudioFormat.Normalized, DateTime.UtcNow);
                await Task.Yield();
            }
        }
    }

    private sealed class FakeTranscribe : ILiveTranscribeClient
    {
        private readonly StreamSource? _failOn;

        public FakeTranscribe(StreamSource? failOn = null) => _failOn = failOn;

        public async IAsyncEnumerable<LiveCaption> StreamCaptionsAsync(
            IAsyncEnumerable<AudioFrame> frames,
            StreamingOptions options,
            [EnumeratorCancellation] CancellationToken ct)
        {
            if (_failOn == options.Source)
            {
                // inner なし InvalidOperationException → ReconnectPolicy.Classify で Fatal（リトライしない）。
                throw new InvalidOperationException($"fake fatal failure: {options.Source}");
            }

            await Task.Yield();
            yield return new LiveCaption(options.Source.Label(), "テスト字幕", IsPartial: true, TimeSpan.Zero);
            yield return new LiveCaption(options.Source.Label(), "テスト字幕確定", IsPartial: false, TimeSpan.FromSeconds(1));
        }
    }

    /// <summary>試行番号（0 始まり）に応じて例外を投げる/成功する合成クライアント。再接続ループの検証用。</summary>
    private sealed class ScriptedTranscribe : ILiveTranscribeClient
    {
        private readonly Func<int, Exception?> _script;
        private int _calls;

        public ScriptedTranscribe(Func<int, Exception?> script) => _script = script;

        public int CallCount => _calls;

        public async IAsyncEnumerable<LiveCaption> StreamCaptionsAsync(
            IAsyncEnumerable<AudioFrame> frames,
            StreamingOptions options,
            [EnumeratorCancellation] CancellationToken ct)
        {
            int attempt = System.Threading.Interlocked.Increment(ref _calls) - 1;
            Exception? toThrow = _script(attempt);
            if (toThrow is not null)
            {
                throw toThrow;
            }

            await Task.Yield();
            yield return new LiveCaption(options.Source.Label(), "テスト字幕", IsPartial: true, TimeSpan.Zero);
            yield return new LiveCaption(options.Source.Label(), "テスト字幕確定", IsPartial: false, TimeSpan.FromSeconds(1));
        }
    }
}
