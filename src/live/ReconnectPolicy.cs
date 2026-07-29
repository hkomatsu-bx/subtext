using System.IO;
using System.Net.Http;
using Amazon.Runtime;
using Amazon.TranscribeStreaming.Model;

namespace Subtext.Live;

/// <summary>
/// 例外の再接続可否分類（FR-A1-01 / BR-RECONN-01）。
/// Transient=一過性（再接続する） / Fatal=恒久（当該系統を諦める）。
/// </summary>
public enum FaultKind
{
    Transient,
    Fatal,
}

/// <summary>
/// 再接続の設定値（NFR-A1-01）。秘密は持たない（BR-SEC-01）。
/// appsettings.json / 環境変数（接頭辞 SUBTEXT_）から束縛し、未設定は既定値。
/// </summary>
public sealed record ReconnectOptions
{
    /// <summary>BR-RECONN-02: 指数バックオフの初期待機。</summary>
    public int InitialBackoffMs { get; init; } = 1000;

    /// <summary>BR-RECONN-02: バックオフ上限（飽和値）。</summary>
    public int MaxBackoffMs { get; init; } = 30000;

    /// <summary>BR-RECONN-03: 連続失敗を恒久障害とみなすウィンドウ（分）。</summary>
    public int MaxReconnectWindowMinutes { get; init; } = 5;

    /// <summary>
    /// FR-A1-05: keep-alive 無音注入の間隔（ミリ秒）。Transcribe の 15s idle timeout 未満に保つ。
    /// この間隔内に capture フレームが届かなければ無音 PCM を1チャンク注入する。0 以下で無効。
    /// </summary>
    public int KeepAliveIntervalMs { get; init; } = 5000;

    /// <summary>
    /// 設定値の妥当性を検証する（起動時 fail-fast）。
    ///
    /// 設定は appsettings / 環境変数から無検証に束縛されるため、負値や 0 がそのまま入り得る。
    /// 負のバックオフは <see cref="ReconnectPolicy.NextDelay"/> が負の <see cref="TimeSpan"/> を返し、
    /// <c>Task.Delay</c> が **再接続経路の内側で** ArgumentOutOfRangeException を投げて系統を落とす
    /// （切断からの復帰という、まさに効いてほしい局面で Unit C が死ぬ）。起動時に弾く。
    /// </summary>
    public void Validate()
    {
        if (InitialBackoffMs <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(InitialBackoffMs), InitialBackoffMs, "Live:Reconnect:InitialBackoffMs は正の値。");
        }

        if (MaxBackoffMs < InitialBackoffMs)
        {
            throw new ArgumentOutOfRangeException(
                nameof(MaxBackoffMs), MaxBackoffMs, "Live:Reconnect:MaxBackoffMs は InitialBackoffMs 以上。");
        }

        if (MaxReconnectWindowMinutes <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(MaxReconnectWindowMinutes),
                MaxReconnectWindowMinutes,
                "Live:Reconnect:MaxReconnectWindowMinutes は正の値。");
        }
    }
}

/// <summary>
/// 再接続の判断ロジック（純粋関数群・副作用なし）。NFR-A1-03＝実 AWS 非依存で単体テストする主対象。
/// オーケストレータ（<see cref="LiveSttApp"/>）から呼ばれ、ここには状態を持たない。
/// </summary>
public static class ReconnectPolicy
{
    // idle timeout（無音で Transcribe がセッションを閉じる）を表すメッセージ断片。
    // メッセージ判定は脆いため定数化し、SDK バージョン差で壊れたら実機ログで再調整する（設計リスク参照）。
    private static readonly string[] IdleTimeoutMarkers =
    {
        "no new audio",
        "timed out",
        "timeout",
    };

    /// <summary>
    /// BR-RECONN-01: 例外を Transient/Fatal に分類する。未知は Transient に倒す（安全側：
    /// 最悪でも予算ぶん再接続して停止する）。<see cref="OperationCanceledException"/> は
    /// 正常停止のため分類対象外（呼び出し側が再接続ループの外で握る）。
    /// </summary>
    public static FaultKind Classify(Exception ex)
    {
        ArgumentNullException.ThrowIfNull(ex);

        // StreamCaptionsAsync は実原因を InvalidOperationException でラップする。内側で再分類する。
        if (ex is InvalidOperationException { InnerException: { } inner })
        {
            return Classify(inner);
        }

        return ex switch
        {
            // idle timeout は一過性（無音注入で回避するが、競合で漏れたら再接続）。
            BadRequestException bre when ContainsIdleMarker(bre.Message) => FaultKind.Transient,

            // 上記以外の不正リクエストはリトライ無意味。
            BadRequestException => FaultKind.Fatal,

            // サーバ一過性（5xx 相当）。
            InternalFailureException => FaultKind.Transient,
            ServiceUnavailableException => FaultKind.Transient,

            // 認証/権限はリトライ無意味（資格情報問題）。
            AmazonServiceException ase when IsAuthFailure(ase) => FaultKind.Fatal,

            // それ以外の Amazon 例外は 5xx 相当を一過性に倒す。
            AmazonServiceException ase => IsServerError(ase) ? FaultKind.Transient : FaultKind.Fatal,

            // ネットワーク断・I/O・タイムアウト（ct 非由来）は一過性。
            HttpRequestException => FaultKind.Transient,
            IOException => FaultKind.Transient,
            TimeoutException => FaultKind.Transient,
            TaskCanceledException => FaultKind.Transient,

            // 設定/論理エラー（デバイス不在など、inner を持たない InvalidOperationException）は恒久。
            InvalidOperationException => FaultKind.Fatal,

            // 未知は Transient（BR-RECONN-01 既定）。
            _ => FaultKind.Transient,
        };
    }

    /// <summary>
    /// BR-RECONN-02: フルジッタ指数バックオフ。<paramref name="jitter01"/> は [0,1) の注入値
    /// （テスト決定化のため外部注入）。delay = jitter01 * min(maxBackoff, initial * 2^attempt)。
    /// </summary>
    public static TimeSpan NextDelay(int attempt, ReconnectOptions opt, double jitter01)
    {
        ArgumentNullException.ThrowIfNull(opt);
        if (attempt < 0)
        {
            throw new ArgumentOutOfRangeException(nameof(attempt), attempt, "attempt は 0 以上。");
        }

        // 2^attempt の桁あふれを避けるため、上限到達分は早期に飽和させる。
        double ceiling = opt.MaxBackoffMs;
        double exp = attempt >= 30 ? double.MaxValue : opt.InitialBackoffMs * Math.Pow(2, attempt);
        double capped = Math.Min(ceiling, exp);
        double clampedJitter = Math.Clamp(jitter01, 0.0, 1.0);
        return TimeSpan.FromMilliseconds(clampedJitter * capped);
    }

    /// <summary>
    /// BR-RECONN-03: 連続失敗ウィンドウの予算判定。最初の失敗（<paramref name="windowStartUtc"/>）
    /// からの経過が <see cref="ReconnectOptions.MaxReconnectWindowMinutes"/> を超えたら恒久障害。
    /// 字幕受信成功で呼び出し側が windowStart をクリアする（予算リセット）。
    /// </summary>
    public static bool IsBudgetExhausted(DateTime windowStartUtc, DateTime nowUtc, ReconnectOptions opt)
    {
        ArgumentNullException.ThrowIfNull(opt);
        TimeSpan window = TimeSpan.FromMinutes(opt.MaxReconnectWindowMinutes);
        return nowUtc - windowStartUtc > window;
    }

    private static bool ContainsIdleMarker(string? message)
    {
        if (string.IsNullOrEmpty(message))
        {
            return false;
        }

        foreach (string marker in IdleTimeoutMarkers)
        {
            if (message.Contains(marker, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
    }

    private static bool IsAuthFailure(AmazonServiceException ex) =>
        ex.StatusCode is System.Net.HttpStatusCode.Unauthorized or System.Net.HttpStatusCode.Forbidden;

    // StatusCode 未設定(0)＝HTTP 応答を得る前の接続断等。「未知は Transient」の既定（BR-RECONN-01）と
    // 揃えて一過性に倒す（最悪でも予算ぶん再接続して停止する）。
    private static bool IsServerError(AmazonServiceException ex) =>
        ex.StatusCode == 0 || (int)ex.StatusCode >= 500;
}
