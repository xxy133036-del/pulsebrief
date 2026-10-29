import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings, get_settings


def _send_message(message: EmailMessage, settings: Settings) -> None:
    if settings.smtp_port == 465:
        connection = smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=15,
            context=ssl.create_default_context(),
        )
    else:
        connection = smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=15
        )

    with connection as server:
        if settings.smtp_port != 465 and settings.smtp_starttls:
            server.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)


def send_login_code(email: str, code: str) -> None:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_from:
        raise RuntimeError(
            "Email sign-in is unavailable until SMTP_HOST and SMTP_FROM are configured."
        )
    message = EmailMessage()
    message["Subject"] = "每日 AI 新闻助手登录验证码"
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(
        f"你的登录验证码是 {code}。验证码 10 分钟内有效。未申请登录时请忽略此邮件。\n"
    )
    _send_message(message, settings)


def send_briefing_ready(email: str, title: str, web_url: str) -> None:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_from:
        raise RuntimeError("Email reminders are unavailable until SMTP is configured.")
    safe_title = " ".join(title.replace("\r", " ").replace("\n", " ").split())[:180]
    message = EmailMessage()
    message["Subject"] = f"每日 AI 新闻简报已生成：{safe_title}"
    message["From"] = settings.smtp_from
    message["To"] = email
    message.set_content(
        "你订阅的 AI 新闻简报已经生成。为保护你的阅读内容，邮件中不包含订阅关键词或简报正文。\n\n"
        f"打开网站查看：{web_url.rstrip('/')}\n\n"
        "如果你不希望继续收到提醒，可以在网站的账户设置中关闭邮件提醒。\n"
    )
    _send_message(message, settings)
