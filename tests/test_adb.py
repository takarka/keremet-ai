from devices import adb


def test_network_serial_detection():
    assert adb.is_network_serial("192.168.1.119:5555")
    assert not adb.is_network_serial("af54415c")
    assert not adb.is_network_serial(None)


def test_usb_serial_needs_no_connect(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен вызываться")))
    assert adb.ensure_connected("af54415c") is True
    assert adb.ensure_connected(None) is True


class _Out:
    def __init__(self, stdout):
        self.stdout = stdout


def test_connect_results(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Out("already connected to 1.2.3.4:5555"))
    assert adb.ensure_connected("1.2.3.4:5555") is True
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Out("failed to connect to '1.2.3.4:5555': Connection refused"))
    assert adb.ensure_connected("1.2.3.4:5555") is False
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Out("cannot connect to 1.2.3.4:5555: timed out"))
    assert adb.ensure_connected("1.2.3.4:5555") is False
