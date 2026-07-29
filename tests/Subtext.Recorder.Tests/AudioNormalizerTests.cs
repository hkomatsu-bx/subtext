using Subtext.Capture;
using Subtext.Recorder;
using Xunit;

namespace Subtext.Recorder.Tests;

public sealed class AudioNormalizerTests
{
    private readonly AudioNormalizer _normalizer = new();

    /// <summary>単一フレームを初期状態で正規化するヘルパ。</summary>
    private AudioFrame NormalizeOne(AudioFrame raw) =>
        _normalizer.Normalize(raw, ResampleState.Initial).Frame;

    [Fact]
    public void Normalize_StereoPcm16_DownmixesToMonoAverage()
    {
        // Arrange: 1 ステレオサンプルフレーム L=1000, R=2000 → mono 期待 1500
        var pcm = new byte[4];
        BitConverter.GetBytes((short)1000).CopyTo(pcm, 0);
        BitConverter.GetBytes((short)2000).CopyTo(pcm, 2);
        var raw = new AudioFrame(pcm, new AudioFormat(16000, 2, 16, SampleType.Pcm), DateTime.UtcNow);

        // Act
        AudioFrame result = NormalizeOne(raw);

        // Assert
        Assert.Equal(AudioFormat.Normalized, result.Format);
        short mono = BitConverter.ToInt16(result.Pcm, 0);
        Assert.InRange(mono, 1499, 1501);
    }

    [Fact]
    public void Normalize_48kToTarget_ResamplesToRoughlyOneThird()
    {
        // Arrange: 480 サンプルの 48kHz mono 16bit → 16kHz で約 160 サンプル
        const int inputSamples = 480;
        var pcm = new byte[inputSamples * 2];
        var raw = new AudioFrame(pcm, new AudioFormat(48000, 1, 16, SampleType.Pcm), DateTime.UtcNow);

        // Act
        AudioFrame result = NormalizeOne(raw);

        // Assert
        Assert.Equal(16000, result.Format.SampleRate);
        int outputSamples = result.Pcm.Length / 2;
        Assert.InRange(outputSamples, (inputSamples / 3) - 2, (inputSamples / 3) + 2);
    }

    [Fact]
    public void Normalize_ConsecutiveFrames_CarriesStateWithoutTruncationDrift()
    {
        // Arrange: 48kHz mono 16bit、100 サンプル×3 フレーム（1/3 で割り切れない長さ）。
        // フレーム独立の切り捨てだと 33×3=99 サンプルに縮むが、状態持ち越しで合計 100 になる。
        var format = new AudioFormat(48000, 1, 16, SampleType.Pcm);
        ResampleState state = ResampleState.Initial;
        int totalSamples = 0;

        // Act
        for (int i = 0; i < 3; i++)
        {
            var raw = new AudioFrame(new byte[100 * 2], format, DateTime.UtcNow);
            (AudioFrame frame, state) = _normalizer.Normalize(raw, state);
            totalSamples += frame.Pcm.Length / 2;
        }

        // Assert: 300 ソースサンプル ÷ 3 = 100 出力サンプル（累積ドリフトなし）
        Assert.Equal(100, totalSamples);
    }

    [Fact]
    public void Normalize_Float32OutOfRange_SaturatesTo16BitBounds()
    {
        // Arrange: +2.0(>1) と -2.0(<-1) → 飽和して 32767 / -32767
        var pcm = new byte[8];
        BitConverter.GetBytes(2.0f).CopyTo(pcm, 0);
        BitConverter.GetBytes(-2.0f).CopyTo(pcm, 4);
        var raw = new AudioFrame(pcm, new AudioFormat(16000, 1, 32, SampleType.IeeeFloat), DateTime.UtcNow);

        // Act
        AudioFrame result = NormalizeOne(raw);

        // Assert
        Assert.Equal((short)32767, BitConverter.ToInt16(result.Pcm, 0));
        Assert.Equal((short)-32767, BitConverter.ToInt16(result.Pcm, 2));
    }

    [Fact]
    public void Normalize_DoesNotMutateInputFrame()
    {
        // Arrange
        var pcm = new byte[] { 1, 2, 3, 4 };
        var raw = new AudioFrame(pcm, new AudioFormat(16000, 1, 16, SampleType.Pcm), DateTime.UtcNow);

        // Act
        AudioFrame result = NormalizeOne(raw);

        // Assert: 入力は変更されない（immutability）
        Assert.False(ReferenceEquals(raw.Pcm, result.Pcm));
        Assert.Equal(new byte[] { 1, 2, 3, 4 }, raw.Pcm);
        Assert.Equal(16, raw.Format.BitDepth);
    }

    [Fact]
    public void Normalize_UnsupportedFormat_Throws()
    {
        // Arrange: 24bit PCM は非対応
        var raw = new AudioFrame(new byte[6], new AudioFormat(16000, 1, 24, SampleType.Pcm), DateTime.UtcNow);

        // Act / Assert
        Assert.Throws<NotSupportedException>(() => NormalizeOne(raw));
    }
}
