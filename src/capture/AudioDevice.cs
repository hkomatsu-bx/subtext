namespace Subtext.Capture;

/// <summary>
/// オーディオデバイスの方向。Render=再生(ループバック元)、Capture=録音(マイク)。
/// </summary>
public enum DeviceDirection
{
    Render,
    Capture
}

/// <summary>
/// 取得対象のオーディオデバイス（FR-06）。capture は列挙・選択のみ行い、変更しない。
/// イミュータブルな値として扱う。
/// </summary>
/// <param name="Id">OS が提供する安定したデバイス識別子。</param>
/// <param name="Name">表示名。設定での名前指定に用いる（FR-15）。</param>
/// <param name="Direction">再生(Render) か 録音(Capture) か。</param>
/// <param name="IsDefault">当該方向の既定デバイスかどうか（Q4=A の自動選択根拠）。</param>
public sealed record AudioDevice(
    string Id,
    string Name,
    DeviceDirection Direction,
    bool IsDefault);
