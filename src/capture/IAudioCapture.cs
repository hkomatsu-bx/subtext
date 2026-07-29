namespace Subtext.Capture;

/// <summary>
/// 音声キャプチャの抽象（Unit A / Unit C 共用, Q2=A）。
/// 実装はハードウェア取得に専念し、正規化・同期は行わない（責務分離, Q5=A）。
/// テストでは合成実装に差し替え可能にするための seam。
/// </summary>
public interface IAudioCapture
{
    /// <summary>利用可能なオーディオデバイスを列挙する（FR-06）。</summary>
    IReadOnlyList<AudioDevice> ListDevices();

    /// <summary>
    /// 指定した再生デバイスのループバック（相手=others）を取得する（FR-01）。
    /// 再生音が無い区間はフレームが供給されない点に注意（FR-04 同期で補填）。
    /// </summary>
    IAsyncEnumerable<AudioFrame> CaptureLoopback(AudioDevice outputDevice, CancellationToken ct);

    /// <summary>指定した録音デバイス（自分=self、マイク）を取得する（FR-02）。</summary>
    IAsyncEnumerable<AudioFrame> CaptureMicrophone(AudioDevice inputDevice, CancellationToken ct);
}
