using Subtext.Live;
using Xunit;

namespace Subtext.Live.Tests;

/// <summary>
/// <see cref="ReconnectOptions.Validate"/> の検証。設定は appsettings / 環境変数から無検証に
/// 束縛されるため、不正値は起動時に弾く必要がある（再接続経路の内側で落ちると復帰できない）。
/// </summary>
public sealed class ReconnectOptionsValidationTests
{
    [Fact]
    public void Validate_AcceptsDefaults()
    {
        new ReconnectOptions().Validate(); // 既定値は妥当（例外を投げない）
    }

    [Fact]
    public void Validate_RejectsNegativeInitialBackoff()
    {
        // 負のバックオフは NextDelay が負の TimeSpan を返し、Task.Delay が再接続待機の内側で
        // ArgumentOutOfRangeException を投げて系統を落とす（切断からの復帰時にだけ発現する）。
        var opt = new ReconnectOptions { InitialBackoffMs = -1 };

        Assert.Throws<ArgumentOutOfRangeException>(() => opt.Validate());
    }

    [Fact]
    public void Validate_RejectsZeroInitialBackoff()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => new ReconnectOptions { InitialBackoffMs = 0 }.Validate());
    }

    [Fact]
    public void Validate_RejectsMaxBackoffBelowInitial()
    {
        var opt = new ReconnectOptions { InitialBackoffMs = 5_000, MaxBackoffMs = 1_000 };

        Assert.Throws<ArgumentOutOfRangeException>(() => opt.Validate());
    }

    [Fact]
    public void Validate_RejectsNonPositiveWindow()
    {
        Assert.Throws<ArgumentOutOfRangeException>(
            () => new ReconnectOptions { MaxReconnectWindowMinutes = 0 }.Validate());
    }

    [Fact]
    public void NextDelay_IsNeverNegative_ForValidatedOptions()
    {
        var opt = new ReconnectOptions();
        opt.Validate();

        for (var attempt = 0; attempt < 40; attempt++)
        {
            Assert.True(ReconnectPolicy.NextDelay(attempt, opt, 0.0) >= TimeSpan.Zero);
            Assert.True(ReconnectPolicy.NextDelay(attempt, opt, 1.0) >= TimeSpan.Zero);
        }
    }
}
