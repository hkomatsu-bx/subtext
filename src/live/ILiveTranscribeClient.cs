using Subtext.Capture;

namespace Subtext.Live;

/// <summary>
/// ライブ字幕の抽象境界（L2 送信 / L3 受信）。1系統の AudioFrame ストリームを
/// Transcribe Streaming へ中継し、字幕イベントを返す（FR-12）。
/// テストではこの IF を合成実装に差し替える seam（実 AWS を呼ばない）。
/// </summary>
public interface ILiveTranscribeClient
{
    /// <summary>
    /// 指定系統のフレームをストリーミングし、partial/final の字幕を逐次返す。
    /// 切断・API エラーは握りつぶさず例外送出（fail-fast, Q6=A / BR-ERR-01）。
    /// </summary>
    IAsyncEnumerable<LiveCaption> StreamCaptionsAsync(
        IAsyncEnumerable<AudioFrame> frames,
        StreamingOptions options,
        CancellationToken ct);
}
