"""Ghost distribution channel using the Ghost Admin API."""

from __future__ import annotations

import base64
from datetime import datetime
from datetime import timezone
import hashlib
import hmac
import json
import logging
import re
from typing import Any, Iterable

import httpx

from bcn.common.secrets import redact_error_text
from bcn.common.url_policy import assert_public_http_url
from bcn.common.url_policy import normalize_trusted_hosts
from bcn.common.url_policy import URLValidationError
from bcn.services.writer.rendering import render_html_body

logger = logging.getLogger(__name__)

_BODY_TAG_RE = re.compile(r"(?is)<body[^>]*>(.*?)</body>")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _strip_document_shell(html_text: str) -> str:
    body_match = _BODY_TAG_RE.search(str(html_text or ""))
    if body_match:
        return body_match.group(1).strip()
    return str(html_text or "").strip()


def _build_ghost_html(briefing: Any) -> str:
    markdown = str(briefing.get("content_markdown") or "").strip()
    if markdown:
        return render_html_body(markdown)
    return _strip_document_shell(str(briefing.get("content_html") or ""))


class GhostDistributor:
    """Publishes briefings to Ghost via the Admin API."""

    def __init__(
        self,
        admin_api_url: str,
        admin_api_key: str,
        trusted_image_hosts: Iterable[str] | None = None,
    ) -> None:
        self.admin_api_url = self._normalize_admin_api_url(admin_api_url)
        self.admin_api_key = str(admin_api_key or "").strip()
        self._trusted_image_hosts = normalize_trusted_hosts(trusted_image_hosts)
        self._redaction_secrets = (self.admin_api_key,)
        self._client = httpx.AsyncClient(timeout=30)
        self.last_result: dict[str, Any] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, briefing: Any) -> bool:
        self.last_result = {}
        try:
            body_html = _build_ghost_html(briefing)
            if not body_html:
                raise ValueError("empty Ghost post body")

            title = self._extract_title(briefing)
            payload: dict[str, Any] = {
                "title": title,
                "html": body_html,
                "status": "published",
            }

            feature_image_url = await self._prepare_feature_image(briefing)
            feature_image_error = str(self.last_result.get("feature_image_error") or "")
            if feature_image_url:
                payload["feature_image"] = feature_image_url

            resp = await self._client.post(
                f"{self.admin_api_url}/posts/?source=html",
                headers=self._build_headers(),
                json={"posts": [payload]},
            )
            resp.raise_for_status()
            response_json = resp.json()
            post = ((response_json or {}).get("posts") or [{}])[0]
            post_url = str(post.get("url") or "").strip()
            post_id = str(post.get("id") or post.get("uuid") or "").strip()
            primary = post_url or post_id or "ok"
            self.last_result = {
                "ok": True,
                "post_id": post_id,
                "post_url": post_url,
                "status": post.get("status"),
                "primary_message_id": primary,
                "feature_image_url": payload.get("feature_image", ""),
                "response": response_json,
            }
            if feature_image_error:
                self.last_result["feature_image_error"] = feature_image_error
            return True
        except Exception as exc:
            safe_error = redact_error_text(exc, secrets=self._redaction_secrets)
            logger.error("Ghost publish failed: %s", safe_error)
            self.last_result = {"error": safe_error}
            return False

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Ghost {self._build_jwt()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Version": "v6.0",
        }

    def _build_jwt(self) -> str:
        key_id, secret_hex = self._split_admin_key()
        header = {"alg": "HS256", "kid": key_id, "typ": "JWT"}
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {"iat": now, "exp": now + 300, "aud": "/admin/"}
        signing_input = ".".join(
            (
                _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
                _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            )
        )
        signature = hmac.new(
            bytes.fromhex(secret_hex),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return signing_input + "." + _b64url(signature)

    def _split_admin_key(self) -> tuple[str, str]:
        parts = self.admin_api_key.split(":", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("Ghost Admin API key must be in '<id>:<secret>' format")
        return parts[0], parts[1]

    @staticmethod
    def _normalize_admin_api_url(raw_url: str) -> str:
        base = str(raw_url or "").strip().rstrip("/")
        if not base:
            return ""
        if base.endswith("/ghost/api/admin"):
            return base
        return base + "/ghost/api/admin"

    async def _prepare_feature_image(self, briefing: dict[str, Any]) -> str:
        cover_image_url = str(briefing.get("cover_image_url") or "").strip()
        if not cover_image_url:
            return ""

        try:
            filename, mime_type, image_bytes = await self._load_cover_image_bytes(
                cover_image_url
            )
            uploaded_url = await self._upload_image(
                filename=filename,
                mime_type=mime_type,
                image_bytes=image_bytes,
            )
            return uploaded_url
        except Exception as exc:
            safe_error = redact_error_text(exc, secrets=self._redaction_secrets)
            logger.warning("Ghost feature image upload skipped: %s", safe_error)
            self.last_result["feature_image_error"] = safe_error
            return ""

    async def _load_cover_image_bytes(
        self, cover_image_url: str
    ) -> tuple[str, str, bytes]:
        if cover_image_url.startswith("data:image/"):
            return self._decode_data_image_uri(cover_image_url)

        try:
            assert_public_http_url(
                cover_image_url,
                trusted_hosts=self._trusted_image_hosts,
            )
        except URLValidationError as exc:
            raise ValueError(f"Blocked cover image URL: {exc}") from exc

        image_response = await self._client.get(cover_image_url, timeout=30)
        image_response.raise_for_status()
        mime_type = (
            image_response.headers.get("content-type", "image/png").split(";")[0].strip()
            or "image/png"
        )
        extension = mime_type.rsplit("/", 1)[-1] if "/" in mime_type else "png"
        return f"cover.{extension}", mime_type, image_response.content

    async def _upload_image(
        self,
        *,
        filename: str,
        mime_type: str,
        image_bytes: bytes,
    ) -> str:
        response = await self._client.post(
            f"{self.admin_api_url}/images/upload/",
            headers={
                "Authorization": f"Ghost {self._build_jwt()}",
                "Accept": "application/json",
                "Accept-Version": "v6.0",
            },
            data={"purpose": "image", "ref": filename},
            files={"file": (filename, image_bytes, mime_type)},
        )
        response.raise_for_status()
        response_json = response.json()
        image = ((response_json or {}).get("images") or [{}])[0]
        uploaded_url = str(image.get("url") or "").strip()
        if not uploaded_url:
            raise ValueError("Ghost image upload returned no URL")
        return uploaded_url

    def _extract_title(self, briefing: dict[str, Any]) -> str:
        subject = str(briefing.get("email_subject") or "").strip()
        created_at = briefing.get("created_at")
        if subject:
            if isinstance(created_at, datetime):
                return f"{subject} ({self._format_created_time(created_at)})"
            return subject
        if isinstance(created_at, datetime):
            return f"Broken Cloud Update - {self._format_created_time(created_at)}"
        briefing_id = str(briefing.get("id") or "").strip()
        if briefing_id:
            return f"Broken Cloud Update #{briefing_id[:8]}"
        return "Broken Cloud Update"

    @staticmethod
    def _format_created_time(created_at: datetime) -> str:
        value = created_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    @staticmethod
    def _decode_data_image_uri(value: str) -> tuple[str, str, bytes]:
        header, sep, payload = value.partition(",")
        if not sep or ";base64" not in header:
            raise ValueError("Unsupported data URI format for cover image")
        mime_type = (
            header[5 : header.index(";")] if header.startswith("data:") else "image/png"
        )
        raw = base64.b64decode(payload)
        extension = mime_type.rsplit("/", 1)[-1] if "/" in mime_type else "png"
        return f"cover.{extension}", mime_type, raw
