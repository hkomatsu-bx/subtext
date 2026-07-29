using Subtext.Capture;

namespace Subtext.Recorder;

/// <summary>
/// L1 デバイス決定（FR-06, Q4=A, BR-DEV-01..03）。
/// self は Capture 方向、others は Render 方向から選ぶ。名前指定があれば完全一致、
/// 無ければ既定デバイスを採用する。方向や名前の不整合はエラーで早期に弾く。
/// </summary>
public sealed class DeviceResolver
{
    /// <summary>self(マイク) と others(ループバック元) を決定する。</summary>
    public (AudioDevice Self, AudioDevice Others) Resolve(
        IReadOnlyList<AudioDevice> devices,
        string? selfName,
        string? othersName)
    {
        ArgumentNullException.ThrowIfNull(devices);

        AudioDevice self = ResolveOne(devices, DeviceDirection.Capture, selfName, "self/microphone");
        AudioDevice others = ResolveOne(devices, DeviceDirection.Render, othersName, "others/loopback");
        return (self, others);
    }

    private static AudioDevice ResolveOne(
        IReadOnlyList<AudioDevice> devices,
        DeviceDirection direction,
        string? name,
        string label)
    {
        var candidates = devices.Where(d => d.Direction == direction).ToList();
        if (candidates.Count == 0)
        {
            // BR-DEV-01: 当該方向のデバイスが無い
            throw new InvalidOperationException($"No {direction} device available for {label}.");
        }

        if (!string.IsNullOrWhiteSpace(name))
        {
            // BR-DEV-02: 名前は完全一致（部分一致は曖昧回避のため不可）
            var matched = candidates.Where(d => string.Equals(d.Name, name, StringComparison.Ordinal)).ToList();
            if (matched.Count == 0)
            {
                throw new InvalidOperationException($"No {direction} device named '{name}' for {label}.");
            }

            if (matched.Count > 1)
            {
                throw new InvalidOperationException($"Multiple {direction} devices named '{name}' for {label}.");
            }

            return matched[0];
        }

        // BR-DEV-03: 名前指定が無ければ既定デバイス。既定が無ければ先頭。
        return candidates.FirstOrDefault(d => d.IsDefault) ?? candidates[0];
    }
}
