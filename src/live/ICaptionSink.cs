namespace Subtext.Live;

/// <summary>
/// 確定字幕の永続化シンク（B1F・FR-B1F-01/02）。呼び出し側は <b>final のみ</b>渡す（partial は渡さない）。
/// 未設定（null 実装を注入しない）なら永続化しない＝従来のコンソール専用挙動。
/// 字幕本文は PII を含むためログに出さずファイルのみへ書く（NFR-B1F-02）。
/// </summary>
public interface ICaptionSink : IDisposable
{
    /// <summary>確定字幕 1 件を追記する。</summary>
    void Append(LiveCaption caption, StreamSource source);
}
