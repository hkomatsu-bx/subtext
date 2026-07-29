using Subtext.Capture;
using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

public sealed class LivePcmConverterTests
{
    private static byte[] FloatStereoBytes(params (float left, float right)[] frames)
    {
        var bytes = new byte[frames.Length * 2 * sizeof(float)];
        for (int i = 0; i < frames.Length; i++)
        {
            BitConverter.GetBytes(frames[i].left).CopyTo(bytes, (i * 2) * sizeof(float));
            BitConverter.GetBytes(frames[i].right).CopyTo(bytes, ((i * 2) + 1) * sizeof(float));
        }

        return bytes;
    }

    private static AudioFrame StereoFloatFrame(int sampleRate, params (float, float)[] frames) =>
        new(FloatStereoBytes(frames), new AudioFormat(sampleRate, 2, 32, SampleType.IeeeFloat),
            DateTime.UtcNow);

    /// <summary>単一フレームを初期状態で変換するヘルパ。</summary>
    private static byte[] ConvertOne(LivePcmConverter converter, AudioFrame frame) =>
        converter.ToPcm16Mono16k(frame, ResampleState.Initial).Pcm;

    [Fact]
    public void ToPcm16Mono16k_DownmixesStereoToMono()
    {
        // Arrange: 16k なのでリサンプルなし。2 サンプルフレーム。
        var converter = new LivePcmConverter(16000);
        AudioFrame frame = StereoFloatFrame(16000, (0.5f, 0.5f), (0.25f, 0.75f));

        // Act
        byte[] pcm = ConvertOne(converter, frame);

        // Assert: mono 2 サンプル = 4 バイト。1つ目 0.5 → 16384。
        Assert.Equal(4, pcm.Length);
        short first = BitConverter.ToInt16(pcm, 0);
        Assert.Equal((short)Math.Round(0.5f * 32767f), first);
    }

    [Fact]
    public void ToPcm16Mono16k_SaturatesOutOfRange()
    {
        var converter = new LivePcmConverter(16000);
        AudioFrame frame = StereoFloatFrame(16000, (1.5f, 1.5f), (-2.0f, -2.0f));

        byte[] pcm = ConvertOne(converter, frame);

        Assert.Equal((short)32767, BitConverter.ToInt16(pcm, 0));   // +飽和
        Assert.Equal((short)-32767, BitConverter.ToInt16(pcm, 2));  // -飽和
    }

    [Fact]
    public void ToPcm16Mono16k_ResamplesDownTo16k()
    {
        // 48k → 16k は 1/3。6 サンプル → 約2サンプル(4バイト)。
        var converter = new LivePcmConverter(16000);
        var frames = new (float, float)[]
        {
            (0f, 0f), (0.1f, 0.1f), (0.2f, 0.2f), (0.3f, 0.3f), (0.4f, 0.4f), (0.5f, 0.5f),
        };
        AudioFrame frame = StereoFloatFrame(48000, frames);

        byte[] pcm = ConvertOne(converter, frame);

        Assert.Equal(2 * 2, pcm.Length); // 2 サンプル × 2バイト
    }

    [Fact]
    public void ToPcm16Mono16k_ConsecutiveFrames_CarryResampleState()
    {
        // Arrange: 48kHz、100 サンプル×3 フレーム（1/3 で割り切れない長さ）。
        // フレーム独立の切り捨てだと 33×3=99 サンプルに縮むが、状態持ち越しで合計 100 になる。
        var converter = new LivePcmConverter(16000);
        var samples = new (float, float)[100];
        ResampleState state = ResampleState.Initial;
        int totalSamples = 0;

        // Act
        for (int i = 0; i < 3; i++)
        {
            AudioFrame frame = StereoFloatFrame(48000, samples);
            (byte[] pcm, state) = converter.ToPcm16Mono16k(frame, state);
            totalSamples += pcm.Length / 2;
        }

        // Assert: 300 ソースサンプル ÷ 3 = 100 出力サンプル（累積ドリフトなし）
        Assert.Equal(100, totalSamples);
    }

    [Fact]
    public void ToPcm16Mono16k_DoesNotMutateInput()
    {
        var converter = new LivePcmConverter(16000);
        AudioFrame frame = StereoFloatFrame(16000, (0.5f, 0.5f));
        byte[] original = (byte[])frame.Pcm.Clone();

        ConvertOne(converter, frame);

        Assert.Equal(original, frame.Pcm); // 入力不変（immutability）
    }
}
