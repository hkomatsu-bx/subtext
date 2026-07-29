using System.Globalization;

namespace Subtext.Recorder;

/// <summary>
/// セッションID（`yyyyMMdd-HHmmss`）の生成（Q6=A / BR-IO-01）。書式は下流との契約。
/// </summary>
public static class SessionId
{
    /// <summary>セッションID の書式。下流（会議ハーネスの既定セッション解決・Slack の日時タイトル）が前提にする。</summary>
    private const string Format = "yyyyMMdd-HHmmss";

    /// <summary>
    /// ローカル時刻からセッションIDを作る（純粋）。
    ///
    /// **InvariantCulture 固定**。既定カルチャに従うと Windows の地域設定が和暦のとき
    /// `080729-134503`（令和 8 年）のような 6 桁年になり、桁数と時系列の単調性が壊れる。
    /// ディレクトリ名がそのままセッションIDになるため、下流は日時表示にも順序判定にも
    /// この書式を前提にしている。
    /// </summary>
    public static string FromLocalTime(DateTime localNow) => localNow.ToString(Format, CultureInfo.InvariantCulture);
}
