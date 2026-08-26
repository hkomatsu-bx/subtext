using System.Buffers;
using System.Runtime.InteropServices;
using Subtext.Capture;

namespace Subtext.Recorder;

/// <summary>
/// 線形補間リサンプルのフレーム間連続性状態。ストリーム（系統）ごとに保持して呼び戻すことで、
/// フレーム単位の切り捨てによる累積ドリフト（→無音マイクロ挿入）と境界不連続を防ぐ。
/// </summary>
/// <param name="Position">次に出力すべきサンプルの、現フレーム先頭を 0 とするソース位置（端数含む。負値は前フレーム末尾との補間）。</param>
/// <param name="PrevSample">直前フレームの末尾サンプル（Position が負のときの補間相手）。</param>
public readonly record struct ResampleState(double Position, float PrevSample)
{
    /// <summary>ストリーム先頭の初期状態。</summary>
    public static ResampleState Initial => default;
}

/// <summary>
/// L4 正規化（FR-05, BR-FMT-01..04）。native 生フレームを Transcribe 適合の
/// 16kHz / mono / 16bit PCM へ変換する。順序固定: ダウンミックス → リサンプル → 量子化。
/// 入力は変更せず新フレームを返す（immutability）。本クラスは状態を持たずスレッド安全で、
/// リサンプルの連続性状態は <see cref="ResampleState"/> として呼び出し側がストリームごとに保持する。
///
/// 中間の float バッファは <see cref="ArrayPool{T}"/> から借りる（10ms フレームが毎秒 100 回
/// 通る経路で、借りた配列はこのメソッドの外へ出ない）。最終的な PCM バイト列だけは
/// <see cref="AudioFrame"/> として下流（WAV 書込）へ渡るため、プールに載せず新規確保する。
/// </summary>
public sealed class AudioNormalizer
{
    private const int TargetSampleRate = 16000;

    /// <summary>生フレームを正規化し、新しい <see cref="AudioFrame"/> と更新後のリサンプル状態を返す。</summary>
    public (AudioFrame Frame, ResampleState State) Normalize(AudioFrame raw, ResampleState state)
    {
        ArgumentNullException.ThrowIfNull(raw);

        int frameCount = PcmFormat.FrameCount(raw.Format, raw.Pcm.Length);
        float[] monoBuffer = ArrayPool<float>.Shared.Rent(frameCount + 1); // +1: 0 サンプルでも借りられる
        try
        {
            Span<float> mono = monoBuffer.AsSpan(0, frameCount);
            PcmFormat.ToMonoFloat(raw.Pcm, raw.Format, mono);               // step1: ダウンミックス

            // 同一レート・空フレームはリサンプルせず状態も進めない（連続性状態は次フレームへ持ち越す）。
            if (raw.Format.SampleRate == TargetSampleRate || frameCount == 0)
            {
                return (Normalized(raw, PcmFormat.ToPcm16(mono)), state);
            }

            int outCount = PcmFormat.ResampledCount(frameCount, raw.Format.SampleRate, TargetSampleRate, state.Position);
            float[] outBuffer = ArrayPool<float>.Shared.Rent(outCount + 1);
            try
            {
                Span<float> resampled = outBuffer.AsSpan(0, outCount);
                ResampleState next = PcmFormat.Resample(                     // step2: リサンプル
                    mono, resampled, raw.Format.SampleRate, TargetSampleRate, state);
                return (Normalized(raw, PcmFormat.ToPcm16(resampled)), next); // step3: 量子化(飽和)
            }
            finally
            {
                ArrayPool<float>.Shared.Return(outBuffer);
            }
        }
        finally
        {
            ArrayPool<float>.Shared.Return(monoBuffer);
        }
    }

    private static AudioFrame Normalized(AudioFrame raw, byte[] pcm16) =>
        raw with { Pcm = pcm16, Format = AudioFormat.Normalized };
}
