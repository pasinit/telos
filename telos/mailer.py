import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


@dataclass(frozen=True)
class EmailConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    from_addr: str = ""


def send_email(config: EmailConfig, to: str, subject: str, body: str, html_body: str | None = None) -> None:
    """Send an email using the deployment SMTP settings."""
    if not config.host:
        raise RuntimeError("SMTP is not configured.")
    if not to.strip():
        raise ValueError("Recipient email address is required.")
    if not subject.strip():
        raise ValueError("Email subject is required.")
    if not body.strip():
        raise ValueError("Email body is required.")

    from_addr = config.from_addr or config.user
    if not from_addr:
        raise RuntimeError("SMTP sender address is not configured.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.attach(MIMEText(body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(config.host, config.port) as smtp:
        smtp.starttls()
        if config.user or config.password:
            smtp.login(config.user, config.password)
        smtp.send_message(msg)
