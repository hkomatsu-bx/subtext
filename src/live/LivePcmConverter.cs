using System.Buffers;
using Subtext.Capture;

namespace Subtext.Live;

/// <summary>
/// 線形補間リサンプルのフレーム間連続性状態。ストリーム（接続セッション）ごとに保持して呼び戻すことで、
/// フレーム単位の切り捨てによる累積ドリフトと境界不連続を防ぐ。
/// </summary>
/// <param name="Position">次に出力すべきサンプルの、現フレーム先頭を 0 とするソース位置（端数含む。負値は前フレーム末尾との補間）。</param>
/// <param name="PrevSample">直前フレームの末尾サンプル（Position が負のときの補間相手）。</param>
public readonly record struct ResampleState(double Position, float PrevSample)
{
    /// <summary>ストリーム先頭の初期状態。</summary>
    public static ResampleState Initial => default;
}

/// <summary>
/// ストリーミング向け正規化（DG-A）。capture の native フレームを Transcribe Streaming 適合の
/// 16kHz / mono / 16bit PCM へ変換する。順序固定: ダウンミックス → リサンプル → 量子化(飽和)。
/// 入力は変更せず新バイト列を返す（純粋・immutability）。本クラスは状態を持たずスレッド安全で、
/// リサンプルの連続性状態は <see cref="ResampleState"/> として呼び出し側がセッションごとに保持する。
///
/// 中間の float バッファは <see cref="ArrayPool{T}"/> から借りる（借りた配列はこのメソッドの外へ
/// 出ない）。送信する PCM バイト列だけは呼び出し側（Channel → SDK）へ渡るため新規確保する。
///
/// 注: Unit A の <c>AudioNormalizer</c> と同一アルゴリズム。共有 capture には正規化が無く、
/// recorder への依存を避けるため Unit C 内に同順序で実装する（DG-A）。
/// </summary>
public sealed class LivePcmConverter(int targetSampleRate = 16000)
{
    private readonly int _targetSampleRate = targetSampleRate > 0
        ? targetSampleRate
        : throw new ArgumentOutOfRangeException(nameof(targetSampleRate));

    /// <summary>native フレームを 16kHz/mono/16bit PCM のバイト列へ変換し、更新後のリサンプル状態を返す。</summary>
    public (byte[] Pcm, ResampleState State) ToPcm16Mono16k(AudioFrame raw, ResampleState state)
    {
        ArgumentNullException.ThrowIfNull(raw);

        int frameCount = PcmFormat.FrameCount(raw.Format, raw.Pcm.Length);
        float[] monoBuffer = ArrayPool<float>.Shared.Rent(frameCount + 1); // +1: 0 サンプルでも借りられる
        try
        {
            Span<float> mono = monoBuffer.AsSpan(0, frameCount);
            PcmFormat.ToMonoFloat(raw.Pcm, raw.Format, mono);              // step1: ダウンミックス

            // 同一レート・空フレームはリサンプルせず状態も進めない（連続性状態は次フレームへ持ち越す）。
            if (raw.Format.SampleRate == _targetSampleRate || frameCount == 0)
            {
                return (PcmFormat.ToPcm16(mono), state);
            }

            int outCount = PcmFormat.ResampledCount(
                frameCount, raw.Format.SampleRate, _targetSampleRate, state.Position);
            float[] outBuffer = ArrayPool<float>.Shared.Rent(outCount + 1);
            try
            {
                Span<float> resampled = outBuffer.AsSpan(0, outCount);
                ResampleState next = PcmFormat.Resample(                    // step2: リサンプル
                    mono, resampled, raw.Format.SampleRate, _targetSampleRate, state);
                return (PcmFormat.ToPcm16(resampled), next);                // step3: 量子化(飽和)
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
}
