using Microsoft.Extensions.Configuration;

namespace Subtext.Recorder;

/// <summary>
/// recorder の設定（FR-15, Q4=A/Q1=B）。境界の DTO のため可変プロパティで束縛する。
/// 認証情報は持たない・直書きしない（BR-SEC-01, NFR-SEC-07）。
/// </summary>
public sealed class RecorderConfig
{
    /// <summary>AWS リージョン（Unit A では未使用。将来の連携整合のため保持）。</summary>
    public string AwsRegion { get; set; } = "ap-northeast-1";

    /// <summary>self(マイク) のデバイス名。未指定なら既定デバイス（BR-DEV-03）。</summary>
    public string? InputDevice { get; set; }

    /// <summary>others(ループバック元) のデバイス名。未指定なら既定デバイス（BR-DEV-03）。</summary>
    public string? OutputDevice { get; set; }

    /// <summary>録音出力のルートディレクトリ。</summary>
    public string OutputDir { get; set; } = "recordings";

    /// <summary>最大録音時間（分）。上限到達で自動停止（Q1=B, BR-STOP-02）。</summary>
    public int MaxDurationMinutes { get; set; } = 180;

    /// <summary>停止シグナルファイルのパス（任意, env: SUBTEXT_Recorder__StopFile）。
    /// 出現で graceful 停止する（H2: Claude 駆動の on-demand 停止用。シェル signal 非依存）。</summary>
    public string? StopFile { get; set; }
}

/// <summary>設定ローダ。appsettings.json → 環境変数(SUBTEXT_ 接頭辞) → コマンドライン引数 の順で
/// 重ねて束縛する。優先順位は後勝ち（CLI 引数 &gt; 環境変数 &gt; appsettings.json &gt; 既定値）。
/// 既存の env 経路は温存し、CLI 引数は併用・上書き用（FR-15, Q4=A）。</summary>
public static class RecorderConfigLoader
{
    /// <summary>短縮スイッチ → Recorder セクションのキー写像。
    /// 写像のない `--Recorder:MaxDurationMinutes=2` 形式も同時に有効。</summary>
    private static readonly Dictionary<string, string> SwitchMappings = new()
    {
        ["-m"] = "Recorder:MaxDurationMinutes",
        ["--minutes"] = "Recorder:MaxDurationMinutes",
        ["-o"] = "Recorder:OutputDir",
        ["--out"] = "Recorder:OutputDir",
        ["--stop-file"] = "Recorder:StopFile",
        ["--input"] = "Recorder:InputDevice",
        ["--output"] = "Recorder:OutputDevice",
    };

    public static RecorderConfig Load(string[]? args = null, string fileName = "appsettings.json")
    {
        IConfigurationBuilder builder = new ConfigurationBuilder()
            .SetBasePath(AppContext.BaseDirectory)
            .AddJsonFile(fileName, optional: true, reloadOnChange: false)
            .AddEnvironmentVariables(prefix: "SUBTEXT_");

        // CLI 引数を最後に重ねる（env より優先）。引数が無ければ何も足さない＝後方互換。
        if (args is { Length: > 0 })
        {
            builder.AddCommandLine(args, SwitchMappings);
        }

        IConfigurationRoot configuration = builder.Build();
        var config = new RecorderConfig();
        configuration.GetSection("Recorder").Bind(config);
        Validate(config);
        return config;
    }

    /// <summary>束縛後の妥当性検証（fail-fast）。不正値は原因が分かるメッセージで起動時に弾く。</summary>
    private static void Validate(RecorderConfig config)
    {
        if (config.MaxDurationMinutes <= 0)
        {
            throw new InvalidOperationException(
                $"Config 'Recorder:MaxDurationMinutes' must be a positive integer (got {config.MaxDurationMinutes}).");
        }

        if (string.IsNullOrWhiteSpace(config.OutputDir))
        {
            throw new InvalidOperationException("Config 'Recorder:OutputDir' must not be empty.");
        }
    }
}
