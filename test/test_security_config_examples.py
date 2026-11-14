"""Keep shipped configuration examples on the secure TLS/network defaults."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_tls_examples_keep_certificate_verification_enabled():
    for name in (".env.example", "README.md", "README_fa.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "VERIFY_TLS=true" in text
        assert "VERIFY_TLS=false" not in text or name == "README.md"  # troubleshooting warning only


def test_docker_quickstart_is_loopback_published_and_authenticated():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "REMS_HOST=0.0.0.0" in dockerfile
    assert "-p 127.0.0.1:5000:5000" in readme
    assert "REMS_ACCESS_TOKEN" in readme
    assert "REMS_ALLOWED_HOSTS" in readme


def test_headless_docs_explain_loopback_ssh_tunnel_and_opt_in_network_bind():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ssh -L 5000:127.0.0.1:5000" in readme
    assert "REMS_ALLOWED_HOSTS" in readme
    assert "database/rems_access_token" in readme
