namespace Subtext.Capture;

/// <summary>系統の役割。self=自分(マイク)、others=相手(ループバック)。連携契約に直結。</summary>
public enum StreamRole
{
    Self,
    Others
}

/// <summary>サンプルの数値表現。WASAPI は IeeeFloat(32bit) を返すことが多い。</summary>
public enum SampleType
{
    Pcm,
    IeeeFloat
}

/// <summary>
/// 音声フォーマット（値オブジェクト）。
/// </summary>
public sealed record AudioFormat(
    int SampleRate,
    int Channels,
    int BitDepth,
    SampleType SampleType)
{
    /// <summary>Transcribe 適合の正規化済フォーマット（16kHz/mono/16bit PCM, FR-05）。</summary>
    public static readonly AudioFormat Normalized = new(16000, 1, 16, SampleType.Pcm);
}

/// <summary>
/// キャプチャされた音声の最小チャンク。生(native)と正規化済の2状態を取りうる。
/// イミュータブル: 正規化は新インスタンスを返し、入力は変更しない（coding-style）。
/// </summary>
/// <param name="Pcm">音声サンプル列（生は native 形式 / 正規化済は 16bit PCM リトルエンディアン）。</param>
/// <param name="Format">このフレームのフォーマット。</param>
/// <param name="CaptureUtc">先頭サンプルの捕捉時刻。同期の唯一の真実（Q2=A, BR-SYNC-01）。</param>
public sealed record AudioFrame(
    byte[] Pcm,
    AudioFormat Format,
    DateTime CaptureUtc);
