using Subtext.Capture;
using Subtext.Recorder;
using Xunit;

namespace Subtext.Recorder.Tests;

public sealed class SyncRecorderTests : IDisposable
{
    private static readonly DateTime T0 = new(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
    private const int SamplesPer100Ms = 1600; // 16kHz
    private const int BytesPer100Ms = SamplesPer100Ms * 2;

    private readonly string _tempDir =
        Path.Combine(Path.GetTempPath(), "subtext-test-" + Guid.NewGuid().ToString("N"));

    private static AudioFrame Frame(int offsetMs)
    {
        var pcm = new byte[BytesPer100Ms]; // 100ms 無音相当（長さのみ重要）
        DateTime captureUtc = T0.AddMilliseconds(offsetMs);
        return new AudioFrame(pcm, AudioFormat.Normalized, captureUtc);
    }

    /// <summary>0 以外のサンプルを含む 100ms フレーム（BR-QLT-03 の「信号あり」側）。</summary>
    private static AudioFrame SignalFrame(int offsetMs)
    {
        var pcm = new byte[BytesPer100Ms];
        pcm[0] = 0x34;
        pcm[1] = 0x12;
        return new AudioFrame(pcm, AudioFormat.Normalized, T0.AddMilliseconds(offsetMs));
    }

    private static async IAsyncEnumerable<AudioFrame> Stream(IEnumerable<AudioFrame> frames)
    {
        foreach (AudioFrame f in frames)
        {
            yield return f;
            await Task.Yield();
        }
    }

    private RecorderOptions Options() => new(
        OutputDir: _tempDir,
        SessionId: "session1",
        MaxDuration: TimeSpan.FromMinutes(180),
        SelfDevice: new AudioDevice("mic", "Mic", DeviceDirection.Capture, true),
        OthersDevice: new AudioDevice("spk", "Speakers", DeviceDirection.Render, true));

    [Fact]
    public async Task RecordAsync_LoopbackGap_FillsSilenceAndAligns()
    {
        // Arrange
        // self: 連続 5 フレーム（0,100,200,300,400ms）
        var selfFrames = new[] { Frame(0), Frame(100), Frame(200), Frame(300), Frame(400) };
        // others: 先頭(0ms)の後、1秒の欠落をはさんで 1100ms に再開
        var othersFrames = new[] { Frame(0), Frame(1100) };

        var recorder = new SyncRecorder(new AudioNormalizer());

        // Act
        RecordingManifest manifest = await recorder.RecordAsync(
            Stream(selfFrames), Stream(othersFrames), Options(), CancellationToken.None, T0);

        // Assert
        Assert.Equal(RecordingStatus.Complete, manifest.Status);
        Assert.Equal(2, manifest.Streams.Count);

        SidecarMeta selfMeta = manifest.Streams.Single(s => s.StreamRole == StreamRole.Self);
        SidecarMeta othersMeta = manifest.Streams.Single(s => s.StreamRole == StreamRole.Others);

        // 連続 self は補填なし
        Assert.Equal(0.0, selfMeta.SilenceFilledSec, precision: 3);
        // others は 1秒分の欠落を無音補填
        Assert.Equal(1.0, othersMeta.SilenceFilledSec, precision: 3);
        Assert.Equal(16000, othersMeta.SampleRate);
        Assert.Equal(1, othersMeta.Channels);
        Assert.Equal(16, othersMeta.BitDepth);
        Assert.Equal(T0, manifest.CommonStartUtc);
    }

    [Fact]
    public async Task RecordAsync_WritesFixedNamedArtifacts()
    {
        var recorder = new SyncRecorder(new AudioNormalizer());

        await recorder.RecordAsync(
            Stream(new[] { Frame(0) }), Stream(new[] { Frame(0) }), Options(), CancellationToken.None, T0);

        string dir = Path.Combine(_tempDir, "session1");
        Assert.True(File.Exists(Path.Combine(dir, "self.wav")));
        Assert.True(File.Exists(Path.Combine(dir, "others.wav")));
        Assert.True(File.Exists(Path.Combine(dir, "self.meta.json")));
        Assert.True(File.Exists(Path.Combine(dir, "others.meta.json")));
        Assert.True(File.Exists(Path.Combine(dir, "manifest.json")));

        // WAV は 44 バイトの有効ヘッダ＋データを持つ
        var wav = new FileInfo(Path.Combine(dir, "self.wav"));
        Assert.True(wav.Length >= 44);
    }

    [Fact]
    public async Task RecordAsync_StreamFault_MarksIncompleteAndSavesPartial()
    {
        async IAsyncEnumerable<AudioFrame> Faulting()
        {
            yield return Frame(0);
            await Task.Yield();
            throw new InvalidOperationException("device disconnected");
        }

        var recorder = new SyncRecorder(new AudioNormalizer());

        var ex = await Assert.ThrowsAsync<RecordingException>(() =>
            recorder.RecordAsync(Faulting(), Stream(new[] { Frame(0) }), Options(), CancellationToken.None, T0));

        Assert.IsType<InvalidOperationException>(ex.InnerException);

        // 部分保存: マニフェストは incomplete で保存されている
        string manifestPath = Path.Combine(_tempDir, "session1", "manifest.json");
        Assert.True(File.Exists(manifestPath));
        RecordingManifest saved = RecordingJson.Deserialize<RecordingManifest>(File.ReadAllText(manifestPath));
        Assert.Equal(RecordingStatus.Incomplete, saved.Status);
    }

    [Fact]
    public async Task RecordAsync_ExistingSessionDirectory_ThrowsBeforeRecording()
    {
        // Arrange: 同一 sessionId の出力先が既に存在する（同一秒での多重起動・再実行）
        string dir = Path.Combine(_tempDir, "session1");
        Directory.CreateDirectory(dir);
        string existing = Path.Combine(dir, "self.wav");
        await File.WriteAllTextAsync(existing, "既存の録音");

        var recorder = new SyncRecorder(new AudioNormalizer());

        // Act
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            recorder.RecordAsync(
                Stream(new[] { Frame(0) }), Stream(new[] { Frame(0) }), Options(), CancellationToken.None, T0));

        // Assert: 既存ファイルは壊されず、メッセージにフルパスを含めない（BR-IO-01 / BR-ERR-04）
        Assert.Equal("既存の録音", await File.ReadAllTextAsync(existing));
        Assert.Contains("session1", ex.Message);
        Assert.DoesNotContain(_tempDir, ex.Message);
    }

    [Fact]
    public async Task RecordAsync_OthersAllSilence_Warns()
    {
        var warnings = new List<string>();
        var recorder = new SyncRecorder(new AudioNormalizer(), warnings.Add);

        await recorder.RecordAsync(
            Stream(new[] { SignalFrame(0) }),
            Stream(new[] { Frame(0), Frame(100) }), // others は全区間 0
            Options(),
            CancellationToken.None,
            T0);

        Assert.Contains(warnings, w => w.Contains("BR-QLT-03"));
    }

    [Fact]
    public async Task RecordAsync_OthersHasSignal_DoesNotWarn()
    {
        var warnings = new List<string>();
        var recorder = new SyncRecorder(new AudioNormalizer(), warnings.Add);

        await recorder.RecordAsync(
            Stream(new[] { Frame(0) }),
            Stream(new[] { Frame(0), SignalFrame(100) }),
            Options(),
            CancellationToken.None,
            T0);

        Assert.Empty(warnings);
    }

    [Fact]
    public async Task RecordAsync_SilenceFillDoesNotCountAsSignal()
    {
        // 無音補填で埋まっただけの系統は「信号あり」に数えない（BR-QLT-03）。
        var warnings = new List<string>();
        var recorder = new SyncRecorder(new AudioNormalizer(), warnings.Add);

        await recorder.RecordAsync(
            Stream(new[] { SignalFrame(0) }),
            Stream(new[] { Frame(0), Frame(1100) }), // 1秒の欠落を無音補填
            Options(),
            CancellationToken.None,
            T0);

        Assert.Contains(warnings, w => w.Contains("BR-QLT-03"));
    }

    [Fact]
    public async Task RecordAsync_LongGap_WarnsAboutInflatedDuration()
    {
        // 時計の跳躍（NTP のステップ補正）・スリープ復帰を模す: 1 フレーム目の直後に 10 分先の
        // captureUtc が来る。絶対位置合わせのため補填自体は正しいが、5 分の録音が 10 分ぶんの
        // WAV と **Transcribe 課金** になるため、黙って通さず警告する。
        var warnings = new List<string>();
        var recorder = new SyncRecorder(new AudioNormalizer(), warnings.Add);

        await recorder.RecordAsync(
            Stream(new[] { SignalFrame(0), SignalFrame(600_000) }), // 10 分ジャンプ
            Stream(new[] { SignalFrame(0) }),
            Options(),
            CancellationToken.None,
            T0);

        Assert.Contains(warnings, w => w.Contains("連続ギャップ") && w.Contains("Self"));
    }

    [Fact]
    public async Task RecordAsync_ShortGap_DoesNotWarnAboutDuration()
    {
        var warnings = new List<string>();
        var recorder = new SyncRecorder(new AudioNormalizer(), warnings.Add);

        await recorder.RecordAsync(
            Stream(new[] { SignalFrame(0), SignalFrame(1100) }), // 1 秒の欠落（通常の取りこぼし）
            Stream(new[] { SignalFrame(0) }),
            Options(),
            CancellationToken.None,
            T0);

        Assert.DoesNotContain(warnings, w => w.Contains("連続ギャップ"));
    }

    public void Dispose()
    {
        if (Directory.Exists(_tempDir))
        {
            Directory.Delete(_tempDir, recursive: true);
        }
    }
}
