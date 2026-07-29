using Subtext.Recorder;
using Xunit;

namespace Subtext.Recorder.Tests;

public sealed class WavWriterTests : IDisposable
{
    private readonly string _path =
        Path.Combine(Path.GetTempPath(), "subtext-wav-" + Guid.NewGuid().ToString("N") + ".wav");

    [Fact]
    public void Finish_AfterWrite_ProducesValidHeaderSizes()
    {
        // Arrange / Act: 320 バイト（10ms 分）書いて確定する。
        using (var writer = new WavWriter(_path))
        {
            writer.WritePcm(new byte[320]);
            writer.Finish();
        }

        // Assert: RIFF チャンクサイズ = 36 + data、data チャンクサイズ = 320。
        byte[] bytes = File.ReadAllBytes(_path);
        Assert.Equal(44 + 320, bytes.Length);
        Assert.Equal(36 + 320, BitConverter.ToInt32(bytes, 4));
        Assert.Equal(320, BitConverter.ToInt32(bytes, 40));
    }

    [Fact]
    public void WriteSilence_ExceedingWavSizeLimit_ThrowsBeforeWriting()
    {
        // Arrange: int.MaxValue サンプル（×2 バイト）は 32bit ヘッダ上限を超える。
        using var writer = new WavWriter(_path);

        // Act / Assert: 書き込む前に上限検査で fail-fast する（巨大ファイルを作らない）。
        Assert.Throws<InvalidOperationException>(() => writer.WriteSilence(int.MaxValue));

        // 既存の書込内容は有効なまま確定できる。
        writer.Finish();
        Assert.Equal(44, new FileInfo(_path).Length);
    }

    public void Dispose()
    {
        if (File.Exists(_path))
        {
            File.Delete(_path);
        }
    }
}
