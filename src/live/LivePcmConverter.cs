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
/// 注: Unit A の <c>AudioNormalizer</c> と同一アルゴリズム。共有 capture には正規化が無く、
/// recorder への依存を避けるため Unit C 内に同順序で実装する（unit-c-code-generation-plan.md DG-A）。
/// </summary>
public sealed class LivePcmConverter
{
    private readonly int _targetSampleRate;

    public LivePcmConverter(int targetSampleRate = 16000)
    {
        if (targetSampleRate <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(targetSampleRate));
        }

        _targetSampleRate = targetSampleRate;
    }

    /// <summary>native フレームを 16kHz/mono/16bit PCM のバイト列へ変換し、更新後のリサンプル状態を返す。</summary>
    public (byte[] Pcm, ResampleState State) ToPcm16Mono16k(AudioFrame raw, ResampleState state)
    {
        ArgumentNullException.ThrowIfNull(raw);

        float[] mono = ToMonoFloat(raw);                                          // step1: ダウンミックス
        (float[] resampled, ResampleState next) =
            Resample(mono, raw.Format.SampleRate, _targetSampleRate, state);      // step2: リサンプル
        return (ToPcm16(resampled), next);                                        // step3: 量子化(飽和)
    }

    /// <summary>全チャンネルを平均して mono の float サンプル列へ変換する。</summary>
    private static float[] ToMonoFloat(AudioFrame raw)
    {
        AudioFormat format = raw.Format;
        int channels = format.Channels;
        if (channels <= 0)
        {
            throw new NotSupportedException($"Invalid channel count: {channels}.");
        }

        int bytesPerSample = format.BitDepth / 8;
        int frameCount = raw.Pcm.Length / (bytesPerSample * channels);
        var mono = new float[frameCount];
        ReadOnlySpan<byte> span = raw.Pcm;

        for (int i = 0; i < frameCount; i++)
        {
            float sum = 0f;
            for (int c = 0; c < channels; c++)
            {
                int offset = (i * channels + c) * bytesPerSample;
                sum += ReadSample(span.Slice(offset, bytesPerSample), format);
            }

            mono[i] = sum / channels;
        }

        return mono;
    }

    /// <summary>1サンプルを [-1.0, 1.0] 規格の float として読む。代表的な WASAPI 形式に対応。</summary>
    private static float ReadSample(ReadOnlySpan<byte> bytes, AudioFormat format)
    {
        if (format.SampleType == SampleType.IeeeFloat && format.BitDepth == 32)
        {
            return BitConverter.ToSingle(bytes);
        }

        if (format.SampleType == SampleType.Pcm && format.BitDepth == 16)
        {
            return BitConverter.ToInt16(bytes) / 32768f;
        }

        if (format.SampleType == SampleType.Pcm && format.BitDepth == 32)
        {
            return BitConverter.ToInt32(bytes) / 2147483648f;
        }

        throw new NotSupportedException($"Unsupported sample format: {format.SampleType} {format.BitDepth}bit.");
    }

    /// <summary>線形補間で srcRate から dstRate へリサンプルする。フレーム境界の端数位置と直前サンプルを
    /// <paramref name="state"/> で持ち越す（切り捨てによる累積ドリフト防止）。同一レートはそのまま返す。</summary>
    private static (float[] Output, ResampleState State) Resample(
        float[] input, int srcRate, int dstRate, ResampleState state)
    {
        if (srcRate == dstRate || input.Length == 0)
        {
            return (input, state);
        }

        double step = (double)srcRate / dstRate; // 出力1サンプルあたりのソース進行量
        double pos = state.Position;
        int n = input.Length;

        int count = pos > n - 1 ? 0 : (int)Math.Floor(((n - 1) - pos) / step) + 1;
        var output = new float[count];

        for (int k = 0; k < count; k++)
        {
            int index = (int)Math.Floor(pos);
            double frac = pos - index;
            float a = index < 0 ? state.PrevSample : input[index];
            float b = index + 1 < n ? input[index + 1] : input[n - 1];
            output[k] = (float)(a + ((b - a) * frac));
            pos += step;
        }

        return (output, new ResampleState(pos - n, input[n - 1]));
    }

    /// <summary>float サンプルを 16bit PCM（リトルエンディアン）へ量子化する。範囲外は飽和。</summary>
    private static byte[] ToPcm16(float[] samples)
    {
        var bytes = new byte[samples.Length * 2];

        for (int i = 0; i < samples.Length; i++)
        {
            float value = samples[i];
            if (value > 1f)
            {
                value = 1f;
            }
            else if (value < -1f)
            {
                value = -1f;
            }

            short sample = (short)Math.Round(value * 32767f);
            bytes[i * 2] = (byte)(sample & 0xFF);
            bytes[(i * 2) + 1] = (byte)((sample >> 8) & 0xFF);
        }

        return bytes;
    }
}
