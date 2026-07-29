using NAudio.Wave;
using Subtext.Capture;
using Xunit;

namespace Subtext.Recorder.Tests;

/// <summary>
/// キャプチャの開始/停止順序の検証。実機 WASAPI は対象外だが、
/// 「停止要求が失われて録音が永久にハングする」経路は NAudio の状態機械を模した
/// <see cref="IWaveIn"/> で再現できるため、ここで固定する（Q7=A の部分保存契約の前提）。
/// </summary>
public sealed class WasapiAudioCaptureTests
{
    /// <summary>取り込み完了を待つ上限（ハングの検出用。正常なら即完了する）。</summary>
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(5);

    /// <summary>
    /// NAudio 2.3.0 の状態機械を模した IWaveIn。
    /// 本物の <c>StopRecording()</c> は <c>captureState</c> が Stopped の間なにもしない
    /// （＝開始前の停止要求は失われる）。この性質がバグの核なので忠実に写す。
    /// </summary>
    private sealed class FakeWaveIn : IWaveIn
    {
        private bool _capturing;

        public WaveFormat WaveFormat { get; set; } = new WaveFormat(16_000, 16, 1);

        public int StartCalls { get; private set; }

        public event EventHandler<WaveInEventArgs>? DataAvailable;

        public event EventHandler<StoppedEventArgs>? RecordingStopped;

        public void StartRecording()
        {
            StartCalls++;
            _capturing = true;
        }

        public void StopRecording()
        {
            if (!_capturing)
            {
                return; // Stopped 中は no-op（本物と同じ）。
            }

            _capturing = false;
            RecordingStopped?.Invoke(this, new StoppedEventArgs());
        }

        public void Emit(byte[] pcm) => DataAvailable?.Invoke(this, new WaveInEventArgs(pcm, pcm.Length));

        public void Dispose()
        {
        }
    }

    private static async Task<int> DrainAsync(FakeWaveIn fake, CancellationToken ct)
    {
        var frames = 0;
        await foreach (var _ in WasapiAudioCapture.CaptureFrames(fake, null, ct).ConfigureAwait(false))
        {
            frames++;
        }

        return frames;
    }

    [Fact]
    public async Task CaptureFrames_DoesNotStartRecording_WhenTokenIsAlreadyCancelled()
    {
        // 片系統の起動失敗で linked.Cancel() が走った直後に、もう一方の系統が呼ばれる状況
        // （SyncRecorder は 2 つの Pump を左から評価するため実際に起きる）。
        // キャンセル登録を StartRecording より前に置くと StopRecording が no-op になり、
        // 誰も完了させない Channel を読み続けて録音が永久にハングする（manifest も書かれない）。
        using var cts = new CancellationTokenSource();
        await cts.CancelAsync();
        var fake = new FakeWaveIn();

        Task<int> pump = Task.Run(() => DrainAsync(fake, cts.Token));
        Task finished = await Task.WhenAny(pump, Task.Delay(Timeout));

        Assert.Same(pump, finished); // ハングしないこと
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => pump);
        Assert.Equal(0, fake.StartCalls); // 止められない録音を始めないこと
    }

    [Fact]
    public async Task CaptureFrames_CompletesAndDeliversFrames_WhenCancelledWhileRunning()
    {
        using var cts = new CancellationTokenSource();
        var fake = new FakeWaveIn();

        Task<int> pump = Task.Run(() => DrainAsync(fake, cts.Token));

        // 開始を待つ（イテレータは列挙開始まで動かない）。
        DateTime deadline = DateTime.UtcNow + Timeout;
        while (fake.StartCalls == 0 && DateTime.UtcNow < deadline)
        {
            await Task.Delay(10);
        }

        Assert.Equal(1, fake.StartCalls);
        fake.Emit(new byte[] { 1, 0, 2, 0 });
        await cts.CancelAsync();

        Task finished = await Task.WhenAny(pump, Task.Delay(Timeout));
        Assert.Same(pump, finished); // 停止要求が届いて完了すること
        Assert.Equal(1, await pump); // 停止前のフレームを取りこぼさないこと
    }
}
