"""Mailbox provider backed by open-source Cloud Mail services / Cloudflare Email Workers.

Supports:
1. Dynamic email generation (random prefix + configured domain, or API account creation)
2. API Authentication via Bearer token / API Key header
3. Polling message list, filtering by keyword and prior IDs
4. Extracting verification code or link from email content
"""

from __future__ import annotations

import logging
import random
import re
import string
import time
from dataclasses import dataclass
from typing import Any

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link

logger = logging.getLogger(__name__)

DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{4,8})(?!\d)"


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _generate_random_email(domain: str) -> str:
    domain_clean = str(domain or "example.com").strip().lstrip("@")
    rand_prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"usr_{rand_prefix}@{domain_clean}"


class CloudMailbox(BaseMailbox):
    def __init__(
        self,
        api_url: str,
        api_token: str = "",
        domain: str = "",
        mode: str = "random",
        poll_interval: int | float = 3,
        request_timeout: int | float = 15,
        allow_reuse: bool = False,
        proxy: str | dict | None = None,
    ):
        self.api_url = str(api_url or "").strip().rstrip("/")
        self.api_token = str(api_token or "").strip()
        self.domain = str(domain or "").strip().lstrip("@")
        self.mode = str(mode or "random").strip().lower()
        self.poll_interval = max(1, int(poll_interval or 3))
        self.request_timeout = max(5, int(request_timeout or 15))
        self.allow_reuse = bool(allow_reuse)

        if isinstance(proxy, str) and proxy:
            self.proxies = {"http": proxy, "https": proxy}
        elif isinstance(proxy, dict):
            self.proxies = proxy
        else:
            self.proxies = None

        self.session = requests.Session()

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "aBaiAutoplus/cloud-mailbox",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
            headers["X-API-Key"] = self.api_token
        return headers

    def peek_email(self) -> str:
        """返回测试所用的预览邮箱地址。"""
        d = self.domain or "cloud-mail.example"
        return f"preview_test@{d}"

    def get_email(self) -> MailboxAccount:
        """获取或生成一个可用邮箱账号。"""
        if not self.api_url:
            raise RuntimeError("Cloud Mail 服务的 API 地址未配置")

        email = ""
        account_id = ""

        # 如果开启了 API 方式创建账号
        if self.mode == "api_create":
            try:
                url = f"{self.api_url}/api/v1/emails/create"
                payload = {"domain": self.domain} if self.domain else {}
                resp = self.session.post(
                    url,
                    json=payload,
                    headers=self._get_headers(),
                    proxies=self.proxies,
                    timeout=self.request_timeout,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    email = data.get("email") or data.get("address") or data.get("mail") or ""
                    account_id = str(data.get("id") or email)
            except Exception as exc:
                logger.warning("Cloud Mail API 创建账号失败，回退到本地随机生成: %s", exc)

        if not email:
            email = _generate_random_email(self.domain or "cloud-mail.local")
            account_id = email

        return MailboxAccount(
            email=email,
            account_id=account_id,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "cloud_mail",
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": {"email": email, "api_url": self.api_url},
                    "metadata": {"source": "cloud_mail"},
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "cloud_mail",
                    "resource_type": "mailbox",
                    "resource_identifier": account_id or email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "source": "cloud_mail",
                    },
                },
            },
        )

    def _fetch_messages(self, account: MailboxAccount) -> list[dict[str, Any]]:
        """向 Cloud Mail API 查询对应邮箱的邮件列表。"""
        email = account.email
        endpoints = [
            f"{self.api_url}/api/v1/messages?email={email}",
            f"{self.api_url}/api/messages?email={email}",
            f"{self.api_url}/api/mails?email={email}",
            f"{self.api_url}/messages?email={email}",
        ]

        last_error = None
        for endpoint in endpoints:
            try:
                resp = self.session.get(
                    endpoint,
                    headers=self._get_headers(),
                    proxies=self.proxies,
                    timeout=self.request_timeout,
                )
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json()

                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    messages = data.get("messages") or data.get("mails") or data.get("data") or data.get("items")
                    if isinstance(messages, list):
                        return messages
                    # 单条邮件
                    if "id" in data or "subject" in data or "text" in data or "html" in data:
                        return [data]
                return []
            except Exception as exc:
                last_error = exc

        logger.debug("获取 Cloud Mail 邮件失败 (%s): %s", email, last_error)
        return []

    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合。"""
        messages = self._fetch_messages(account)
        ids = set()
        for idx, msg in enumerate(messages):
            msg_id = str(msg.get("id") or msg.get("message_id") or msg.get("id_str") or idx)
            ids.add(msg_id)
        return ids

    @classmethod
    def _extract_code_from_msg(cls, msg: dict[str, Any], pattern: re.Pattern[str]) -> str:
        """从单封邮件字典中搜寻验证码。"""
        # 优先提取专属字段
        for field in ("code", "verification_code", "verify_code", "otp"):
            val = str(msg.get(field) or "").strip()
            if val:
                m = pattern.search(val)
                if m:
                    return m.group(1) if m.groups() else m.group(0)

        # 全文搜寻
        text_content = " ".join([
            str(msg.get("subject") or ""),
            str(msg.get("text") or ""),
            str(msg.get("body") or ""),
            str(msg.get("html") or ""),
            str(msg.get("content") or ""),
        ])

        m = pattern.search(text_content)
        if m:
            return m.group(1) if m.groups() else m.group(0)
        return ""

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        """等待并在收到目标邮件后提取验证码。"""
        start_time = time.time()
        before = before_ids or set()
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)

        logger.info("开始在 Cloud Mail (%s) 等待验证码，超时 %d 秒...", account.email, timeout)

        while time.time() - start_time < timeout:
            messages = self._fetch_messages(account)
            for idx, msg in enumerate(messages):
                msg_id = str(msg.get("id") or msg.get("message_id") or msg.get("id_str") or idx)
                if msg_id in before:
                    continue

                full_text = " ".join([
                    str(msg.get("subject") or ""),
                    str(msg.get("text") or ""),
                    str(msg.get("body") or ""),
                    str(msg.get("html") or ""),
                    str(msg.get("content") or ""),
                ]).lower()

                if keyword and keyword.lower() not in full_text:
                    continue

                code = self._extract_code_from_msg(msg, pattern)
                if code:
                    logger.info("在 Cloud Mail (%s) 成功接收验证码: %s", account.email, code)
                    return code

            time.sleep(self.poll_interval)

        raise TimeoutError(f"在 Cloud Mail ({account.email}) 等待验证码超时（{timeout}s）")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        """等待并在收到目标邮件后提取验证链接。"""
        start_time = time.time()
        before = before_ids or set()

        logger.info("开始在 Cloud Mail (%s) 等待验证链接，超时 %d 秒...", account.email, timeout)

        while time.time() - start_time < timeout:
            messages = self._fetch_messages(account)
            for idx, msg in enumerate(messages):
                msg_id = str(msg.get("id") or msg.get("message_id") or msg.get("id_str") or idx)
                if msg_id in before:
                    continue

                full_text = " ".join([
                    str(msg.get("subject") or ""),
                    str(msg.get("text") or ""),
                    str(msg.get("body") or ""),
                    str(msg.get("html") or ""),
                    str(msg.get("content") or ""),
                ])

                link = _extract_verification_link(full_text, keyword=keyword)
                if link:
                    logger.info("在 Cloud Mail (%s) 成功提取验证链接: %s", account.email, link)
                    return link

            time.sleep(self.poll_interval)

        raise TimeoutError(f"在 Cloud Mail ({account.email}) 等待验证链接超时（{timeout}s）")
