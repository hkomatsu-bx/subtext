using Microsoft.Extensions.Configuration;

namespace Subtext.Live;

/// <summary>
/// ライブ字幕の設定（FR-15）。appsettings.json ＋ 環境変数（接頭辞 SUBTEXT_）から束縛する。
/// Unit A の RecorderConfig とは独立（Q5=A）。認証情報は読まない＝AWS プロファイル/SSO/環境変数に委譲（NFR-SEC-07）。
/// </summary>
public sealed record LiveSttConfig(
    string AwsRegion,
    string Language,
    IReadOnlyList<StreamSource> Sources,
    int SampleRate)
{
    private const string Section = "Live";

    /// <summary>再接続の設定（FR-A1 / NFR-A1-01）。未設定は既定値。秘密は持たない（BR-SEC-01）。</summary>
    public ReconnectOptions Reconnect { get; init; } = new();

    /// <summary>
    /// カスタム語彙名（FR-C1-01・任意）。未設定（null/空白）なら語彙なし＝従来動作（BR-VOCAB-02）。
    /// 値は AWS で作成済みの語彙名（`tools/vocab/manage_vocabulary.py` が作る名前）と一致させる。
    /// </summary>
    public string? VocabularyName { get; init; }

    /// <summary>
    /// 確定字幕 JSONL の出力先（B1F・FR-B1F-01）。未設定なら永続化しない＝従来のコンソール専用挙動。
    /// env: SUBTEXT_Live__CaptionSinkPath
    /// </summary>
    public string? CaptionSinkPath { get; init; }

    /// <summary>
    /// 停止シグナル（`.stop`）ファイルのパス（B1F・FR-B1F-03）。未設定なら Ctrl+C のみで停止する。
    /// env: SUBTEXT_Live__StopFilePath
    /// </summary>
    public string? StopFilePath { get; init; }

    /// <summary>設定を読み込み束縛する。必須値の欠落・不正は起動時に例外（fail-fast）。</summary>
    public static LiveSttConfig Load(string? basePath = null)
    {
        IConfigurationRoot config = new ConfigurationBuilder()
            .SetBasePath(basePath ?? AppContext.BaseDirectory)
            .AddJsonFile("appsettings.json", optional: false, reloadOnChange: false)
            .AddEnvironmentVariables(prefix: "SUBTEXT_")
            .Build();

        IConfigurationSection section = config.GetSection(Section);

        string region = Require(section["AwsRegion"], "Live:AwsRegion");
        string language = Require(section["Language"], "Live:Language");
        int sampleRate = ParseSampleRate(section["SampleRate"]);
        IReadOnlyList<StreamSource> sources = ParseSources(section.GetSection("Sources").Get<string[]>());
        ReconnectOptions reconnect = section.GetSection("Reconnect").Get<ReconnectOptions>() ?? new ReconnectOptions();
        reconnect.Validate(); // 起動時 fail-fast（不正値は再接続経路の内側で系統を落とすため）
        string? vocabularyName = NormalizeOptional(section["VocabularyName"]);
        string? captionSinkPath = NormalizeOptional(section["CaptionSinkPath"]);
        string? stopFilePath = NormalizeOptional(section["StopFilePath"]);

        return new LiveSttConfig(region, language, sources, sampleRate)
        {
            Reconnect = reconnect,
            VocabularyName = vocabularyName,
            CaptionSinkPath = captionSinkPath,
            StopFilePath = stopFilePath,
        };
    }

    /// <summary>任意設定値の正規化。空白のみは未設定（null）として扱う。</summary>
    private static string? NormalizeOptional(string? value) =>
        string.IsNullOrWhiteSpace(value) ? null : value.Trim();

    private static string Require(string? value, string key) =>
        string.IsNullOrWhiteSpace(value)
            ? throw new InvalidOperationException($"設定 '{key}' が未設定です。")
            : value;

    private static int ParseSampleRate(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
        {
            return 16000;
        }

        if (!int.TryParse(raw, out int value) || value <= 0)
        {
            throw new InvalidOperationException($"設定 'Live:SampleRate' は正の整数で指定してください: '{raw}'");
        }

        return value;
    }

    private static IReadOnlyList<StreamSource> ParseSources(string[]? raw)
    {
        // 既定は自分・相手の2系統（Q1=A）。
        if (raw is null || raw.Length == 0)
        {
            return new[] { StreamSource.Self, StreamSource.Others };
        }

        var sources = new List<StreamSource>();
        foreach (string item in raw)
        {
            sources.Add(item.Trim().ToLowerInvariant() switch
            {
                "self" => StreamSource.Self,
                "others" => StreamSource.Others,
                _ => throw new InvalidOperationException($"未知の系統指定です: '{item}'（self|others）"),
            });
        }

        return sources;
    }
}
