namespace Subtext.Live;

/// <summary>
/// 字幕系統（domain-entities.md, Q4=A）。相手系統内の話者分離はしない（Unit B/SC-P2 の責務）。
/// ラベルは系統粒度のみ。
/// </summary>
public enum StreamSource
{
    Self,
    Others
}

/// <summary>StreamSource の表示ラベル（FR-12）。</summary>
public static class StreamSourceExtensions
{
    /// <summary>コンソール表示ラベル（"自分" / "相手"）。</summary>
    public static string Label(this StreamSource source) =>
        source == StreamSource.Self ? "自分" : "相手";
}

/// <summary>
/// Transcribe Streaming 起動パラメータ（系統ごとに1つ, Q1=A）。
/// 相手系統でも ShowSpeakerLabels は使わない（Q4=A）。
/// </summary>
public sealed record StreamingOptions(
    StreamSource Source,
    string LanguageCode,
    int SampleRate)
{
    /// <summary>Transcribe Streaming 適合の既定（16kHz/PCM, FR-05 整合）。</summary>
    public static StreamingOptions ForSource(StreamSource source, string languageCode, int sampleRate) =>
        new(source, languageCode, sampleRate);
}

/// <summary>
/// 1字幕イベント（FR-12/13/14）。partial/final を区別する。遅延は表示層が
/// <paramref name="CaptureUtc"/> と受信時刻から算出する（Q3=A）。
/// 字幕テキストは PII を含みうるため永続化・ログ出力しない（NFR-SEC-04）。
/// </summary>
/// <param name="Speaker">系統ラベル（"自分"/"相手", Q4=A）。</param>
/// <param name="Text">認識テキスト（partial は暫定・揺れうる）。</param>
/// <param name="IsPartial">true=暫定 / false=確定（Q2=B: いずれも新規行で出力）。</param>
/// <param name="Offset">セッション開始(0)からの発話開始相対時刻。</param>
/// <param name="CaptureUtc">当該区間の capture 時刻（遅延算出の基準, Q3=A）。取得不能は null。</param>
public sealed record LiveCaption(
    string Speaker,
    string Text,
    bool IsPartial,
    TimeSpan Offset,
    DateTime? CaptureUtc = null);

/// <summary>
/// 遅延観測サンプル（FR-14＝SC-P4 主目的）。final のみ集計（BR-LAT-02）。非永続（コンソールサマリのみ）。
/// </summary>
public sealed record LatencySample(
    StreamSource Source,
    TimeSpan Offset,
    double LatencyMs);
