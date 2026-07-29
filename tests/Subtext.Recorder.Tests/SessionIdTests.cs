using System.Globalization;
using Subtext.Recorder;
using Xunit;

namespace Subtext.Recorder.Tests;

/// <summary>
/// セッションID の書式固定（Q6=A / BR-IO-01）。ディレクトリ名がそのままIDになり、下流は
/// `yyyyMMdd-HHmmss` を前提に日時表示と順序判定を行うため、カルチャに揺らされてはならない。
/// </summary>
public sealed class SessionIdTests
{
    [Fact]
    public void FromLocalTime_FormatsAsGregorianTimestamp()
    {
        Assert.Equal("20260729-134503", SessionId.FromLocalTime(new DateTime(2026, 7, 29, 13, 45, 3)));
    }

    [Fact]
    public void FromLocalTime_IgnoresJapaneseCalendarInCurrentCulture()
    {
        // Windows の地域設定を「日本語（和暦）」にすると既定カルチャの暦が JapaneseCalendar になる。
        // InvariantCulture を指定しないと `080729-134503`（令和 8 年）になり、桁数と単調性が壊れる。
        var jaJp = new CultureInfo("ja-JP");
        jaJp.DateTimeFormat.Calendar = new JapaneseCalendar();
        CultureInfo previous = CultureInfo.CurrentCulture;
        try
        {
            CultureInfo.CurrentCulture = jaJp;

            Assert.Equal("20260729-134503", SessionId.FromLocalTime(new DateTime(2026, 7, 29, 13, 45, 3)));
        }
        finally
        {
            CultureInfo.CurrentCulture = previous;
        }
    }
}
