"""Email distribution channel using aiosmtplib."""

from __future__ import annotations

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging

import aiosmtplib

logger = logging.getLogger(__name__)


class EmailDistributor:
    """Sends briefings as HTML emails via SMTP.

    Attributes:
        smtp_host: SMTP server hostname.
        smtp_port: SMTP server port (typically 587 for STARTTLS).
        smtp_user: SMTP authentication username.
        smtp_password: SMTP authentication password.
        from_addr: Sender email address.
        recipients: List of recipient email addresses.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        from_addr: str,
        recipients: list[str],
    ) -> None:
        self.smtp_host: str = smtp_host
        self.smtp_port: int = smtp_port
        self.smtp_user: str = smtp_user
        self.smtp_password: str = smtp_password
        self.from_addr: str = from_addr
        self.recipients: list[str] = recipients

    async def send(self, subject: str, html_body: str) -> bool:
        """Send an HTML email to all configured recipients.

        Args:
            subject: Email subject line.
            html_body: HTML content for the email body.

        Returns:
            ``True`` if the email was sent successfully.
        """
        if not self.recipients:
            logger.warning("No email recipients configured")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = self.from_addr
            msg["To"] = ", ".join(self.recipients)
            msg["Subject"] = subject
            msg.attach(MIMEText(html_body, "html"))

            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_password,
                start_tls=True,
            )
            return True
        except Exception:
            logger.exception("Email send failed")
            return False
