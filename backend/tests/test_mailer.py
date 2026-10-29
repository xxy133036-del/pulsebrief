import pytest


class FakeSMTP:
    def __init__(self, *_args, **_kwargs):
        self.implicit_tls = _kwargs.get("context") is not None
        self.started_tls = False
        self.login_args = None
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self, **_kwargs):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.message = message


@pytest.mark.parametrize(
    "port, expected_implicit_tls, expected_starttls",
    [(465, True, False), (80, False, True)],
)
def test_send_login_code_uses_configured_smtp_transport(
    monkeypatch, port, expected_implicit_tls, expected_starttls
):
    from app.config import get_settings
    from app.services import mailer

    settings = get_settings().model_copy(
        update={
            "smtp_host": "smtp.example.test",
            "smtp_port": port,
            "smtp_user": "login@example.test",
            "smtp_password": "secret",
            "smtp_from": "login@example.test",
            "smtp_starttls": True,
        }
    )
    clients = []

    def build_fake_client(*args, **kwargs):
        client = FakeSMTP(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(mailer, "get_settings", lambda: settings)
    monkeypatch.setattr(
        mailer.smtplib, "SMTP", build_fake_client
    )
    monkeypatch.setattr(
        mailer.smtplib, "SMTP_SSL", build_fake_client
    )

    mailer.send_login_code("reader@example.test", "123456")

    assert len(clients) == 1
    assert clients[0].implicit_tls is expected_implicit_tls
    assert clients[0].started_tls is expected_starttls
    assert clients[0].login_args == ("login@example.test", "secret")
    assert clients[0].message["To"] == "reader@example.test"
