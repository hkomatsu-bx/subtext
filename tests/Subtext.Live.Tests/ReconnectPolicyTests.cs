using System.IO;
using System.Net;
using System.Net.Http;
using Amazon.Runtime;
using Amazon.TranscribeStreaming.Model;
using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

/// <summary>
/// <see cref="ReconnectPolicy"/> の純ロジック検証（NFR-A1-03＝実 AWS 非依存）。
/// 例外分類・指数バックオフ・予算判定を決定的に確認する。
/// </summary>
public sealed class ReconnectPolicyTests
{
    private static readonly ReconnectOptions DefaultOpt = new();

    // --- Classify（BR-RECONN-01） ---

    [Fact]
    public void Classify_IdleTimeoutBadRequest_IsTransient()
    {
        var ex = new BadRequestException("Your request timed out because no new audio was received for 15 seconds.");
        Assert.Equal(FaultKind.Transient, ReconnectPolicy.Classify(ex));
    }

    [Fact]
    public void Classify_OtherBadRequest_IsFatal()
    {
        var ex = new BadRequestException("Invalid media encoding specified.");
        Assert.Equal(FaultKind.Fatal, ReconnectPolicy.Classify(ex));
    }

    [Fact]
    public void Classify_AuthForbidden_IsFatal()
    {
        var ex = new AmazonServiceException("Access denied") { StatusCode = HttpStatusCode.Forbidden };
        Assert.Equal(FaultKind.Fatal, ReconnectPolicy.Classify(ex));
    }

    [Fact]
    public void Classify_ServerError5xx_IsTransient()
    {
        var ex = new AmazonServiceException("Service unavailable") { StatusCode = HttpStatusCode.ServiceUnavailable };
        Assert.Equal(FaultKind.Transient, ReconnectPolicy.Classify(ex));
    }

    [Fact]
    public void Classify_ServiceExceptionWithoutStatusCode_IsTransient()
    {
        // StatusCode 未設定(0)＝HTTP 応答を得る前の接続断等。「未知は Transient」の既定と揃える。
        var ex = new AmazonServiceException("connection aborted");
        Assert.Equal(FaultKind.Transient, ReconnectPolicy.Classify(ex));
    }

    [Fact]
    public void Classify_NetworkErrors_AreTransient()
    {
        Assert.Equal(FaultKind.Transient, ReconnectPolicy.Classify(new HttpRequestException("connection reset")));
        Assert.Equal(FaultKind.Transient, ReconnectPolicy.Classify(new IOException("stream closed")));
        Assert.Equal(FaultKind.Transient, ReconnectPolicy.Classify(new TimeoutException("read timeout")));
    }

    [Fact]
    public void Classify_DeviceAbsentInvalidOperation_IsFatal()
    {
        // ResolveDefault が投げる inner なしの InvalidOperationException（設定/論理エラー）。
        var ex = new InvalidOperationException("マイク(self) デバイスが見つかりません。");
        Assert.Equal(FaultKind.Fatal, ReconnectPolicy.Classify(ex));
    }

    [Fact]
    public void Classify_WrappedTransient_UnwrapsToTransient()
    {
        // StreamCaptionsAsync は実原因を InvalidOperationException でラップする。内側で分類する。
        var ex = new InvalidOperationException("開始に失敗しました。", new HttpRequestException("reset"));
        Assert.Equal(FaultKind.Transient, ReconnectPolicy.Classify(ex));
    }

    [Fact]
    public void Classify_UnknownException_DefaultsToTransient()
    {
        Assert.Equal(FaultKind.Transient, ReconnectPolicy.Classify(new Exception("???")));
    }

    // --- NextDelay（BR-RECONN-02・フルジッタ指数バックオフ） ---

    [Fact]
    public void NextDelay_FullJitter_GrowsExponentially()
    {
        Assert.Equal(1000, ReconnectPolicy.NextDelay(0, DefaultOpt, 1.0).TotalMilliseconds);
        Assert.Equal(2000, ReconnectPolicy.NextDelay(1, DefaultOpt, 1.0).TotalMilliseconds);
        Assert.Equal(4000, ReconnectPolicy.NextDelay(2, DefaultOpt, 1.0).TotalMilliseconds);
    }

    [Fact]
    public void NextDelay_SaturatesAtMaxBackoff()
    {
        // initial=1000, max=30000 → attempt 5 は 32000 だが上限で飽和。
        Assert.Equal(30000, ReconnectPolicy.NextDelay(5, DefaultOpt, 1.0).TotalMilliseconds);
        Assert.Equal(30000, ReconnectPolicy.NextDelay(40, DefaultOpt, 1.0).TotalMilliseconds); // 桁あふれ防御
    }

    [Fact]
    public void NextDelay_JitterScalesDelay()
    {
        Assert.Equal(0, ReconnectPolicy.NextDelay(3, DefaultOpt, 0.0).TotalMilliseconds);
        Assert.Equal(4000, ReconnectPolicy.NextDelay(3, DefaultOpt, 0.5).TotalMilliseconds); // 0.5 * 8000
    }

    [Fact]
    public void NextDelay_NegativeAttempt_Throws()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => ReconnectPolicy.NextDelay(-1, DefaultOpt, 0.5));
    }

    // --- IsBudgetExhausted（BR-RECONN-03） ---

    [Fact]
    public void IsBudgetExhausted_WithinWindow_False()
    {
        var start = new DateTime(2026, 6, 24, 0, 0, 0, DateTimeKind.Utc);
        Assert.False(ReconnectPolicy.IsBudgetExhausted(start, start.AddMinutes(4), DefaultOpt));
    }

    [Fact]
    public void IsBudgetExhausted_BeyondWindow_True()
    {
        var start = new DateTime(2026, 6, 24, 0, 0, 0, DateTimeKind.Utc);
        Assert.True(ReconnectPolicy.IsBudgetExhausted(start, start.AddMinutes(6), DefaultOpt));
    }
}
