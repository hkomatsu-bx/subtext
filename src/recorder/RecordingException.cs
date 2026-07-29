namespace Subtext.Recorder;

/// <summary>
/// 録音中の致命的異常（デバイス切断・WASAPI排他競合・書込失敗）を表す。
/// fail-fast＋部分保存（Q7=A）後にスローし、CLI が非ゼロ終了する根拠となる。
/// メッセージには秘密情報・PII を含めない（BR-ERR-04, NFR-SEC-04）。
/// </summary>
public sealed class RecordingException : Exception
{
    public RecordingException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}
