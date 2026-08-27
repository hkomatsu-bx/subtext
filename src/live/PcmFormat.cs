using System.Runtime.InteropServices;
using Subtext.Capture;

namespace Subtext.Live;

/// <summary>
/// PCM の読み取り・リサンプル・量子化（純粋関数群。副作用なし＝単体テスト容易）。
///
/// サンプル列の読み書きは <see cref="MemoryMarshal.Cast{TFrom,TTo}(Span{TFrom})"/> で型付きの
/// <see cref="Span{T}"/> にしてから触る。1 サンプルごとに <c>BitConverter</c> を呼ぶと、フォーマット
/// 判定の分岐とバイト境界の計算がサンプル数だけ繰り返される（10ms フレームが毎秒 100 回通る経路）。
/// 形式ごとにループを分け、内側の分岐を無くしてある。
///
/// バイト順はリトルエンディアン前提である（対象は win-x64 のみ。WAV も PCM も LE で、
/// キャストの結果がそのまま書き出せる）。
///
/// 注: Unit A の <c>Subtext.Recorder.PcmFormat</c> と同一アルゴリズムである。ユニット間で共有
/// ライブラリを作らない方針（FR-16 / DG-A）に従い、意図的に各ユニット内へ置いている。
/// 片方を直したらもう片方も直すこと。
/// </summary>
internal static class PcmFormat
{
    /// <summary>バイト長とフォーマットからサンプル（フレーム）数を求める。</summary>
    public static int FrameCount(AudioFormat format, int byteLength)
    {
        int bytesPerSample = format.BitDepth / 8;
        if (format.Channels <= 0 || bytesPerSample <= 0)
        {
            throw new NotSupportedException($"Unsupported audio format: {format.Channels}ch {format.BitDepth}bit.");
        }

        return byteLength / (bytesPerSample * format.Channels);
    }

    /// <summary>全チャンネルを平均して mono の float サンプル列へ書き出す（[-1.0, 1.0] 規格）。</summary>
    public static void ToMonoFloat(ReadOnlySpan<byte> pcm, AudioFormat format, Span<float> mono)
    {
        int channels = format.Channels;
        switch (format)
        {
            case { SampleType: SampleType.IeeeFloat, BitDepth: 32 }:
                DownmixFloat(MemoryMarshal.Cast<byte, float>(pcm), channels, mono);
                break;
            case { SampleType: SampleType.Pcm, BitDepth: 16 }:
                DownmixInt16(MemoryMarshal.Cast<byte, short>(pcm), channels, mono);
                break;
            case { SampleType: SampleType.Pcm, BitDepth: 32 }:
                DownmixInt32(MemoryMarshal.Cast<byte, int>(pcm), channels, mono);
                break;
            default:
                throw new NotSupportedException($"Unsupported sample format: {format.SampleType} {format.BitDepth}bit.");
        }
    }

    private static void DownmixFloat(ReadOnlySpan<float> src, int channels, Span<float> mono)
    {
        if (channels == 1)
        {
            src[..mono.Length].CopyTo(mono); // 単一チャンネルは平均を取る必要がない
            return;
        }

        for (int i = 0; i < mono.Length; i++)
        {
            float sum = 0f;
            ReadOnlySpan<float> frame = src.Slice(i * channels, channels);
            for (int c = 0; c < channels; c++)
            {
                sum += frame[c];
            }

            mono[i] = sum / channels;
        }
    }

    private static void DownmixInt16(ReadOnlySpan<short> src, int channels, Span<float> mono)
    {
        for (int i = 0; i < mono.Length; i++)
        {
            float sum = 0f;
            ReadOnlySpan<short> frame = src.Slice(i * channels, channels);
            for (int c = 0; c < channels; c++)
            {
                sum += frame[c] / 32768f;
            }

            mono[i] = sum / channels;
        }
    }

    private static void DownmixInt32(ReadOnlySpan<int> src, int channels, Span<float> mono)
    {
        for (int i = 0; i < mono.Length; i++)
        {
            float sum = 0f;
            ReadOnlySpan<int> frame = src.Slice(i * channels, channels);
            for (int c = 0; c < channels; c++)
            {
                sum += frame[c] / 2147483648f;
            }

            mono[i] = sum / channels;
        }
    }

    /// <summary>リサンプル後の出力サンプル数（<see cref="Resample"/> と同じ歩進で数える）。</summary>
    public static int ResampledCount(int inputLength, int srcRate, int dstRate, double position)
    {
        double step = (double)srcRate / dstRate;
        return position > inputLength - 1 ? 0 : (int)Math.Floor(((inputLength - 1) - position) / step) + 1;
    }

    /// <summary>
    /// 線形補間で srcRate から dstRate へリサンプルし、更新後の連続性状態を返す。
    /// フレーム境界の端数位置と直前サンプルを <paramref name="state"/> で持ち越す（累積ドリフト防止）。
    /// </summary>
    public static ResampleState Resample(
        ReadOnlySpan<float> input, Span<float> output, int srcRate, int dstRate, ResampleState state)
    {
        double step = (double)srcRate / dstRate; // 出力1サンプルあたりのソース進行量
        double pos = state.Position;
        int n = input.Length;

        for (int k = 0; k < output.Length; k++)
        {
            int index = (int)Math.Floor(pos);
            double frac = pos - index;
            float a = index < 0 ? state.PrevSample : input[index];
            float b = index + 1 < n ? input[index + 1] : input[n - 1];
            output[k] = (float)(a + ((b - a) * frac));
            pos += step;
        }

        return new ResampleState(pos - n, input[n - 1]);
    }

    /// <summary>float サンプルを 16bit PCM（リトルエンディアン）へ量子化する。範囲外は飽和（BR-FMT-03）。</summary>
    public static byte[] ToPcm16(ReadOnlySpan<float> samples)
    {
        var bytes = new byte[samples.Length * sizeof(short)];
        // AsSpan() を挟む（byte[] を直に渡すと ReadOnlySpan のオーバーロードが選ばれる）。
        Span<short> dst = MemoryMarshal.Cast<byte, short>(bytes.AsSpan());
        for (int i = 0; i < samples.Length; i++)
        {
            float value = Math.Clamp(samples[i], -1f, 1f);
            dst[i] = (short)Math.Round(value * 32767f);
        }

        return bytes;
    }
}
