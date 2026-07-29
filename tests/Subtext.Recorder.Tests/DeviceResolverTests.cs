using Subtext.Capture;
using Subtext.Recorder;
using Xunit;

namespace Subtext.Recorder.Tests;

public sealed class DeviceResolverTests
{
    private readonly DeviceResolver _resolver = new();

    private static List<AudioDevice> SampleDevices() =>
    [
        new AudioDevice("mic-1", "Default Mic", DeviceDirection.Capture, IsDefault: true),
        new AudioDevice("mic-2", "USB Mic", DeviceDirection.Capture, IsDefault: false),
        new AudioDevice("spk-1", "Default Speakers", DeviceDirection.Render, IsDefault: true),
        new AudioDevice("spk-2", "Headphones", DeviceDirection.Render, IsDefault: false),
    ];

    [Fact]
    public void Resolve_NoNames_PicksDefaultsForEachDirection()
    {
        (AudioDevice self, AudioDevice others) = _resolver.Resolve(SampleDevices(), null, null);

        Assert.Equal("mic-1", self.Id);
        Assert.Equal(DeviceDirection.Capture, self.Direction);
        Assert.Equal("spk-1", others.Id);
        Assert.Equal(DeviceDirection.Render, others.Direction);
    }

    [Fact]
    public void Resolve_WithExactNames_PicksMatchingDevices()
    {
        (AudioDevice self, AudioDevice others) = _resolver.Resolve(SampleDevices(), "USB Mic", "Headphones");

        Assert.Equal("mic-2", self.Id);
        Assert.Equal("spk-2", others.Id);
    }

    [Fact]
    public void Resolve_UnknownName_Throws()
    {
        Assert.Throws<InvalidOperationException>(
            () => _resolver.Resolve(SampleDevices(), "No Such Mic", null));
    }

    [Fact]
    public void Resolve_NameOfWrongDirection_Throws()
    {
        // "Headphones" は render。self(capture) として要求すると一致なしで失敗（方向検証 BR-DEV-01/02）。
        Assert.Throws<InvalidOperationException>(
            () => _resolver.Resolve(SampleDevices(), "Headphones", null));
    }

    [Fact]
    public void Resolve_NoCaptureDevice_Throws()
    {
        var renderOnly = new List<AudioDevice>
        {
            new("spk-1", "Speakers", DeviceDirection.Render, IsDefault: true),
        };

        Assert.Throws<InvalidOperationException>(
            () => _resolver.Resolve(renderOnly, null, null));
    }

    [Fact]
    public void Resolve_NoDefault_FallsBackToFirstCandidate()
    {
        var devices = new List<AudioDevice>
        {
            new("mic-2", "USB Mic", DeviceDirection.Capture, IsDefault: false),
            new("spk-2", "Headphones", DeviceDirection.Render, IsDefault: false),
        };

        (AudioDevice self, AudioDevice others) = _resolver.Resolve(devices, null, null);

        Assert.Equal("mic-2", self.Id);
        Assert.Equal("spk-2", others.Id);
    }
}
