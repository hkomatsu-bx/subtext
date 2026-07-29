namespace Subtext.Recorder;

/// <summary>
/// 16kHz / mono / 16bit PCM の WAV 書き出し（BR-FMT-01）。逐次書込に対応し、
/// <see cref="Finish"/>（またはDispose）でヘッダのサイズを確定する。これにより
/// 異常時でも部分WAVが有効なファイルとして残る（Q7=A, BR-STOP-03, BR-ERR-02）。
/// </summary>
public sealed class WavWriter : IDisposable
{
    private const int SampleRate = 16000;
    private const short Channels = 1;
    private const short BitsPerSample = 16;
    private const int HeaderSize = 44;

    /// <summary>data チャンクの上限バイト数。RIFF ヘッダのサイズ欄は 32bit のため、
    /// 超過すると負のサイズを持つ壊れた WAV になる（約 18.6 時間で到達）。超過前に fail-fast し、
    /// 書込済みデータは有効な部分 WAV として確定できる状態を保つ。</summary>
    private const long MaxDataBytes = int.MaxValue - (HeaderSize - 8);

    private readonly FileStream _stream;
    private readonly BinaryWriter _writer;
    private long _dataBytes;
    private bool _finalized;

    public WavWriter(string path)
    {
        _stream = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.Read);
        _writer = new BinaryWriter(_stream);
        WriteHeaderPlaceholder();
    }

    /// <summary>正規化済 PCM(16bit LE) を書き込む。</summary>
    public void WritePcm(ReadOnlySpan<byte> pcm)
    {
        EnsureCapacity(pcm.Length);
        _writer.Write(pcm);
        _dataBytes += pcm.Length;
    }

    /// <summary>無音サンプルを書き込む（同期の欠落補填, BR-SYNC-02）。</summary>
    public void WriteSilence(long sampleCount)
    {
        if (sampleCount <= 0)
        {
            return;
        }

        long remaining = sampleCount * (BitsPerSample / 8);
        EnsureCapacity(remaining);
        Span<byte> chunk = stackalloc byte[4096];
        chunk.Clear();
        while (remaining > 0)
        {
            int toWrite = (int)Math.Min(remaining, chunk.Length);
            _writer.Write(chunk[..toWrite]);
            _dataBytes += toWrite;
            remaining -= toWrite;
        }
    }

    /// <summary>書込前のサイズ上限検査。上限超過は書き込まずに例外（呼出側の fail-fast＋部分保存へ）。</summary>
    private void EnsureCapacity(long additionalBytes)
    {
        if (_dataBytes + additionalBytes > MaxDataBytes)
        {
            throw new InvalidOperationException(
                $"WAV data size limit ({MaxDataBytes} bytes) would be exceeded; stop and split the recording.");
        }
    }

    /// <summary>ヘッダのサイズ欄を確定し、フラッシュする。冪等。</summary>
    public void Finish()
    {
        if (_finalized)
        {
            return;
        }

        _writer.Flush();
        _stream.Seek(4, SeekOrigin.Begin);
        _writer.Write((int)(HeaderSize - 8 + _dataBytes)); // RIFF chunk size = 36 + data
        _stream.Seek(40, SeekOrigin.Begin);
        _writer.Write((int)_dataBytes);                    // data chunk size
        _writer.Flush();
        _finalized = true;
    }

    private void WriteHeaderPlaceholder()
    {
        _writer.Write("RIFF"u8);
        _writer.Write(0);                 // placeholder: RIFF size
        _writer.Write("WAVE"u8);
        _writer.Write("fmt "u8);
        _writer.Write(16);                // fmt chunk size (PCM)
        _writer.Write((short)1);          // audio format = PCM
        _writer.Write(Channels);
        _writer.Write(SampleRate);
        _writer.Write(SampleRate * Channels * BitsPerSample / 8); // byte rate
        _writer.Write((short)(Channels * BitsPerSample / 8));     // block align
        _writer.Write(BitsPerSample);
        _writer.Write("data"u8);
        _writer.Write(0);                 // placeholder: data size
    }

    public void Dispose()
    {
        try
        {
            // フェイルセーフ: 明示的に Finish していなくてもヘッダを確定する（部分保存）。
            if (!_finalized)
            {
                Finish();
            }
        }
        finally
        {
            // 確定失敗（ディスクフル等）でもファイルハンドルは必ず解放する。
            _writer.Dispose();
            _stream.Dispose();
        }
    }
}
