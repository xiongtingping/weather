import ssl

import certifi

from official_nwp_https_transport import official_https_context


def test_official_transport_uses_certifi_without_disabling_tls_checks():
    context = official_https_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert certifi.where()
