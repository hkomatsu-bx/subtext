using System.Runtime.CompilerServices;
using System.Threading.Channels;
using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace Subtext.Capture;

/// <summary>
/// NAudio(WASAPI) による音声キャプチャ実装。ハードウェア依存のため単体テスト対象外
/// （実機検証は Build &amp; Test ステージ）。イベント駆動の DataAvailable を
/// Channel 経由で IAsyncEnumerable に変換する。各フレームに捕捉時刻を付与（Q2=A）。
/// </summary>
public sealed class WasapiAudioCapture : IAudioCapture
{
    /// <summary>フレーム中継 Channel の上限（WASAPI 10ms フレーム約10秒分）。消費側（書込/送信）の
    /// 停滞時にメモリが無際限に膨張しないよう bounded とし、満杯時は最古フレームを破棄する。
    /// 破棄による欠落は同期段の無音補填が吸収する（BR-SYNC-02 整合）。</summary>
    private const int FrameChannelCapacity = 1024;

    public IReadOnlyList<AudioDevice> ListDevices()
    {
        using var enumerator = new MMDeviceEnumerator();
        var result = new List<AudioDevice>();

        var defaultRenderId = TryGetDefaultId(enumerator, DataFlow.Render);
        var defaultCaptureId = TryGetDefaultId(enumerator, DataFlow.Capture);

        // MMDevice は COM ラッパのため列挙のたびに明示解放する（リーク防止）。
        AddDevices(enumerator, result, DataFlow.Render, DeviceDirection.Render, defaultRenderId);
        AddDevices(enumerator, result, DataFlow.Capture, DeviceDirection.Capture, defaultCaptureId);

        return result;
    }

    private static void AddDevices(
        MMDeviceEnumerator enumerator,
        List<AudioDevice> result,
        DataFlow flow,
        DeviceDirection direction,
        string? defaultId)
    {
        foreach (MMDevice device in enumerator.EnumerateAudioEndPoints(flow, DeviceState.Active))
        {
            using (device)
            {
                result.Add(new AudioDevice(device.ID, device.FriendlyName, direction, device.ID == defaultId));
            }
        }
    }

    private static string? TryGetDefaultId(MMDeviceEnumerator enumerator, DataFlow flow)
    {
        try
        {
            using var device = enumerator.GetDefaultAudioEndpoint(flow, Role.Multimedia);
            return device.ID;
        }
        catch (Exception)
        {
            // 既定デバイスが無い構成もありうる。null を返し IsDefault=false 扱い。
            return null;
        }
    }

    public IAsyncEnumerable<AudioFrame> CaptureLoopback(AudioDevice outputDevice, CancellationToken ct)
    {
        ArgumentNullException.ThrowIfNull(outputDevice);
        (MMDevice mmDevice, IWaveIn capture) = OpenCapture(outputDevice.Id, loopback: true);
        return CaptureFrames(capture, mmDevice, ct);
    }

    public IAsyncEnumerable<AudioFrame> CaptureMicrophone(AudioDevice inputDevice, CancellationToken ct)
    {
        ArgumentNullException.ThrowIfNull(inputDevice);
        (MMDevice mmDevice, IWaveIn capture) = OpenCapture(inputDevice.Id, loopback: false);
        return CaptureFrames(capture, mmDevice, ct);
    }

    /// <summary>
    /// デバイス解決とキャプチャ生成。デバイス不在は呼出時に即例外（起動時 fail-fast を維持）。
    /// 返した MMDevice / IWaveIn の解放は <see cref="CaptureFrames"/> が担う
    /// （列挙されなかった場合のみ解放されない＝全呼出元は必ず列挙する前提）。
    /// </summary>
    private static (MMDevice Device, IWaveIn Capture) OpenCapture(string deviceId, bool loopback)
    {
        using var enumerator = new MMDeviceEnumerator();
        MMDevice mmDevice = enumerator.GetDevice(deviceId);
        try
        {
            IWaveIn capture = loopback
                ? new WasapiLoopbackCapture(mmDevice)
                : new WasapiCapture(mmDevice);
            return (mmDevice, capture);
        }
        catch
        {
            mmDevice.Dispose();
            throw;
        }
    }

    /// <summary>
    /// キャプチャを開始し、フレームを <see cref="IAsyncEnumerable{T}"/> として供給する。
    /// テストから NAudio の状態機械を模した <see cref="IWaveIn"/> を差し込めるよう internal
    /// （<paramref name="mmDevice"/> は実機以外では null）。
    /// </summary>
    internal static async IAsyncEnumerable<AudioFrame> CaptureFrames(
        IWaveIn capture,
        MMDevice? mmDevice,
        [EnumeratorCancellation] CancellationToken ct)
    {
        var channel = Channel.CreateBounded<AudioFrame>(new BoundedChannelOptions(FrameChannelCapacity)
        {
            SingleReader = true,
            SingleWriter = true,
            FullMode = BoundedChannelFullMode.DropOldest
        });

        var format = ToAudioFormat(capture.WaveFormat);

        void OnDataAvailable(object? sender, WaveInEventArgs e)
        {
            if (e.BytesRecorded <= 0)
            {
                return;
            }

            var buffer = new byte[e.BytesRecorded];
            Array.Copy(e.Buffer, buffer, e.BytesRecorded);
            channel.Writer.TryWrite(new AudioFrame(buffer, format, DateTime.UtcNow));
        }

        void OnRecordingStopped(object? sender, StoppedEventArgs e)
        {
            // 例外があれば伝播させ、無ければ正常完了（手動停止/上限到達）。
            channel.Writer.TryComplete(e.Exception);
        }

        capture.DataAvailable += OnDataAvailable;
        capture.RecordingStopped += OnRecordingStopped;

        try
        {
            // 開始前に既にキャンセル済みなら録音を始めない。開始してから止める形にすると、
            // 片系統の起動失敗で linked.Cancel() が走った直後の呼び出しで下の登録が同期実行され、
            // まだ Stopped の capture に対する StopRecording が **NAudio 側で no-op** になって
            // 停止要求が失われる（誰も channel を完了させないまま読み続けて永久にハングし、
            // 部分保存とマニフェスト出力の契約（Q7=A）が黙って破れる）。
            ct.ThrowIfCancellationRequested();
            capture.StartRecording();

            // キャンセル登録は **StartRecording の後**（Capturing 状態でのみ StopRecording が効く）。
            // 開始直後にキャンセルされた場合は Register が同期実行されるため取りこぼさない。
            await using var registration = ct.Register(() =>
            {
                try
                {
                    capture.StopRecording();
                }
                catch (Exception)
                {
                    // ベストエフォート停止。停止失敗は致命的ではない。
                }
            });

            // 停止は capture.StopRecording -> RecordingStopped -> channel 完了で表現するため、
            // 読み出し自体はキャンセルしない（途中のフレームを取りこぼさない）。
            await foreach (var frame in channel.Reader.ReadAllAsync(CancellationToken.None).ConfigureAwait(false))
            {
                yield return frame;
            }
        }
        finally
        {
            capture.DataAvailable -= OnDataAvailable;
            capture.RecordingStopped -= OnRecordingStopped;
            capture.Dispose();
            mmDevice?.Dispose(); // COM リーク防止（再接続の多い Unit C で蓄積するため必須）
        }
    }

    private static AudioFormat ToAudioFormat(WaveFormat waveFormat)
    {
        var sampleType = waveFormat.Encoding == WaveFormatEncoding.IeeeFloat
            ? SampleType.IeeeFloat
            : SampleType.Pcm;
        return new AudioFormat(waveFormat.SampleRate, waveFormat.Channels, waveFormat.BitsPerSample, sampleType);
    }
}
