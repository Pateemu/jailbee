from jailbee.remote_display import HOST_RDP_PORT
from jailbee.remote_ssh.display_forward import (
    DISPLAY_FORWARD_HOST,
    DISPLAY_FORWARD_PORT,
    is_display_forward,
)


def test_only_the_display_proxy_port_on_loopback_is_a_display_forward():
    assert is_display_forward("127.0.0.1", 13389)
    assert not is_display_forward("127.0.0.1", 13390)
    assert not is_display_forward("127.0.0.1", 22)
    assert not is_display_forward("localhost", 13389)
    assert not is_display_forward("0.0.0.0", 13389)
    assert not is_display_forward("10.0.0.1", 13389)


def test_the_forward_target_is_the_display_proxy_device():
    """The server's allowed port and the proxy device's listen port are one value."""
    assert (DISPLAY_FORWARD_HOST, DISPLAY_FORWARD_PORT) == ("127.0.0.1", HOST_RDP_PORT)
