import sys
from types import SimpleNamespace
from unittest.mock import Mock

from bot.pycord_voice_receive import decrypt_rtp_safely


class FakeDave:
    def __init__(self, *, ready=True, user_ids=()):
        self.ready = ready
        self._user_ids = user_ids
        self.decrypt = Mock()

    def get_user_ids(self):
        return self._user_ids


def _context(dave, *, mapping=None):
    state = SimpleNamespace(
        dave_session=dave,
        ssrc_user_map=dict(mapping or {}),
    )
    client = SimpleNamespace(_connection=state, _add_ssrc=Mock())
    decryptor = SimpleNamespace(
        client=client,
        _decryptor_rtp=Mock(return_value=b"transport-decrypted"),
    )
    packet = SimpleNamespace(ssrc=123, decrypted_data=None)
    return decryptor, packet


def _install_fake_davey(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "davey",
        SimpleNamespace(MediaType=SimpleNamespace(audio="audio")),
    )


def test_drops_dave_payload_until_keys_are_ready(monkeypatch):
    _install_fake_davey(monkeypatch)
    dave = FakeDave(ready=False)
    decryptor, packet = _context(dave)

    assert decrypt_rtp_safely(decryptor, packet) == b""
    assert packet.decrypted_data == b""
    dave.decrypt.assert_not_called()


def test_recovers_missing_ssrc_mapping_from_dave_participants(monkeypatch):
    _install_fake_davey(monkeypatch)
    dave = FakeDave(user_ids=(10, 20))
    dave.decrypt.side_effect = [ValueError("wrong participant"), b"opus"]
    decryptor, packet = _context(dave)

    assert decrypt_rtp_safely(decryptor, packet) == b"opus"
    decryptor.client._add_ssrc.assert_called_once_with(20, 123)
    assert packet.decrypted_data == b"opus"


def test_allows_only_explicit_dave_passthrough_transition(monkeypatch):
    _install_fake_davey(monkeypatch)
    dave = FakeDave()
    dave.decrypt.side_effect = ValueError("UnencryptedWhenPassthroughDisabled")
    decryptor, packet = _context(dave, mapping={123: 10})

    assert decrypt_rtp_safely(decryptor, packet) == b"transport-decrypted"


def test_drops_other_dave_decryption_failures_before_opus(monkeypatch):
    _install_fake_davey(monkeypatch)
    dave = FakeDave()
    dave.decrypt.side_effect = ValueError("NoValidCryptorFound")
    decryptor, packet = _context(dave, mapping={123: 10})

    assert decrypt_rtp_safely(decryptor, packet) == b""
    assert packet.decrypted_data == b""

