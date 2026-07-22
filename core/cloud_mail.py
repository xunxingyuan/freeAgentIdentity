"""Mailbox provider backed by open-source Cloud Mail services / Cloudflare Email Workers.

Supports:
1. Dynamic email generation (random prefix + configured domain, or API account creation)
2. API Authentication via Bearer token / API Key header
3. Polling message list, filtering by keyword and prior IDs
4. Extracting verification code or link from email content
"""

from __future__ import annotations

import hashlib
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


FIRST_NAMES = (
    "james", "john", "robert", "michael", "david", "william", "richard", "joseph", "thomas", "daniel",
    "matthew", "anthony", "mark", "donald", "steven", "paul", "andrew", "joshua", "kenneth", "kevin",
    "brian", "george", "edward", "ronald", "timothy", "jason", "jefrey", "ryan", "jacob", "gary",
    "nicholas", "eric", "jonathan", "stephen", "larrry", "justin", "scott", "brandon", "benjamin", "samuel",
    "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara", "susan", "jessica", "sarah", "karen",
    "lisa", "nancy", "betty", "margaret", "sandra", "ashley", "kimberly", "emily", "donna", "michelle",
)

LAST_NAMES = (
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis", "rodriguez", "martinez",
    "hernandez", "lopez", "gonzalez", "wilson", "anderson", "thomas", "taylor", "moore", "jackson", "martin",
    "lee", "perez", "thompson", "white", "harris", "sanchez", "clark", "ramirez", "lewis", "robinson",
)


def _generate_random_email(domain: str) -> str:
    domain_clean = str(domain or "example.com").strip().lstrip("@")
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    num = random.randint(10, 9999)
    sep = random.choice([".", "_", ""])
    prefix = f"{first}{sep}{last}{num}"
    return f"{prefix}@{domain_clean}"



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
            token_val = self.api_token.strip()
            # Send raw token (as used by Cloud Mail v3.0.0 frontend) and legacy X-API-Key
            headers["Authorization"] = token_val
            headers["X-API-Key"] = token_val
        return headers

    def peek_email(self) -> str:
        d = self.domain or "cloud-mail.example"
        return f"preview_test@{d}"

    def get_email(self) -> MailboxAccount:
        if not self.api_url:
            raise RuntimeError("Cloud Mail API Address Not Configured")

        email = ""
        account_id = ""

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
                logger.warning("Cloud Mail API account creation failed, fallback to local random: %s", exc)

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
        email = account.email

        sep = "&" if "?" in self.api_url else "?"
        endpoints = [
            f"{self.api_url}/api/allEmail/list?accountEmail={email}",
            f"{self.api_url}/api/allEmail/list?toEmail={email}",
            f"{self.api_url}/api/allEmail/list",
            f"{self.api_url}{sep}email={email}",
            f"{self.api_url}/api/v1/messages?email={email}",
            f"{self.api_url}/api/messages?email={email}",
            f"{self.api_url}/api/mails?email={email}",
            f"{self.api_url}/api/v1/mail?email={email}",
            f"{self.api_url}/api/mail?email={email}",
            f"{self.api_url}/messages?email={email}",
        ]

        seen_urls = set()
        unique_endpoints = []
        for url in endpoints:
            if url not in seen_urls:
                seen_urls.add(url)
                unique_endpoints.append(url)

        last_error = None
        for endpoint in unique_endpoints:
            try:
                resp = self.session.get(
                    endpoint,
                    headers=self._get_headers(),
                    proxies=self.proxies,
                    timeout=self.request_timeout,
                )
                if resp.status_code in (404, 401):
                    # Also try with "Bearer " prefix if raw token returned 401/404 on legacy endpoint
                    if self.api_token and not self.api_token.startswith("Bearer "):
                        h_bearer = dict(self._get_headers())
                        h_bearer["Authorization"] = f"Bearer {self.api_token}"
                        resp_b = self.session.get(
                            endpoint,
                            headers=h_bearer,
                            proxies=self.proxies,
                            timeout=self.request_timeout,
                        )
                        if resp_b.status_code == 200:
                            resp = resp_b
                        elif resp.status_code == 404:
                            continue
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json()

                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    raw_data = data.get("data")
                    if isinstance(raw_data, dict):
                        messages = (
                            raw_data.get("list")
                            or raw_data.get("records")
                            or raw_data.get("items")
                            or raw_data.get("messages")
                            or raw_data.get("mails")
                        )
                        if isinstance(messages, list):
                            filtered = []
                            for m in messages:
                                if isinstance(m, dict):
                                    to_addr = str(m.get("toEmail") or m.get("recipient") or "").lower()
                                    if not email or not to_addr or email.lower() in to_addr or to_addr in email.lower():
                                        filtered.append(m)
                            return filtered if filtered else messages
                    messages = (
                        data.get("messages")
                        or data.get("mails")
                        or data.get("data")
                        or data.get("items")
                        or data.get("results")
                    )
                    if isinstance(messages, list):
                        return messages
                    if any(k in data for k in ("id", "code", "subject", "text", "html", "content", "body", "verification_code")):
                        return [data]
                return []
            except Exception as exc:
                last_error = exc

        logger.debug("Cloud Mail fetch messages failed (%s): %s", email, last_error)
        return []

    @classmethod
    def _get_msg_id(cls, msg: dict[str, Any], idx: int) -> str:
        if not isinstance(msg, dict):
            return f"msg_{idx}"
        for key in ("id", "message_id", "id_str", "_id", "mid", "key"):
            val = str(msg.get(key) or "").strip()
            if val:
                return val
        content = f"{msg.get('subject')}|{msg.get('date')}|{msg.get('created_at')}|{msg.get('text')}|{msg.get('html')}|{msg.get('code')}"
        if content.strip("|"):
            return hashlib.md5(content.encode("utf-8")).hexdigest()
        return f"msg_{idx}"

    def get_current_ids(self, account: MailboxAccount) -> set:
        messages = self._fetch_messages(account)
        ids = set()
        for idx, msg in enumerate(messages):
            ids.add(self._get_msg_id(msg, idx))
        return ids

    @classmethod
    def _clean_html(cls, html_text: str) -> str:
        if not html_text:
            return ""
        cleaned = re.sub(r"(?is)<(script|style).*?>.*?</>", " ", html_text)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        return " ".join(cleaned.split())

    @classmethod
    def _extract_code_from_msg(cls, msg: dict[str, Any], pattern: re.Pattern[str]) -> str:
        for field in ("code", "verification_code", "verify_code", "otp"):
            val = str(msg.get(field) or "").strip()
            if val:
                m = pattern.search(val)
                if m:
                    return m.group(1) if m.groups() else m.group(0)

        raw_text = str(msg.get("text") or msg.get("body") or msg.get("content") or "")
        html_raw = str(msg.get("html") or "")
        clean_html = cls._clean_html(html_raw)

        full_text = " ".join([
            str(msg.get("subject") or ""),
            raw_text,
            clean_html,
        ])

        six_digit_pattern = re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")
        m6 = six_digit_pattern.search(full_text)
        if m6:
            return m6.group(1) if m6.groups() else m6.group(0)

        m = pattern.search(full_text)
        if m:
            return m.group(1) if m.groups() else m.group(0)

        if html_raw:
            m_raw = pattern.search(html_raw)
            if m_raw:
                return m_raw.group(1) if m_raw.groups() else m_raw.group(0)

        return ""

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        start_time = time.time()
        before = before_ids or set()
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)

        logger.info("Waiting for code in Cloud Mail (%s), timeout %d s...", account.email, timeout)

        while time.time() - start_time < timeout:
            messages = self._fetch_messages(account)
            for idx, msg in enumerate(messages):
                msg_id = self._get_msg_id(msg, idx)
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
                    logger.info("Successfully received code in Cloud Mail (%s): %s", account.email, code)
                    return code

            time.sleep(self.poll_interval)

        raise TimeoutError(f"Timeout waiting for code in Cloud Mail ({account.email}) ({timeout}s)")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        start_time = time.time()
        before = before_ids or set()

        logger.info("Waiting for link in Cloud Mail (%s), timeout %d s...", account.email, timeout)

        while time.time() - start_time < timeout:
            messages = self._fetch_messages(account)
            for idx, msg in enumerate(messages):
                msg_id = self._get_msg_id(msg, idx)
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
                    logger.info("Successfully received link in Cloud Mail (%s): %s", account.email, link)
                    return link

            time.sleep(self.poll_interval)

        raise TimeoutError(f"Timeout waiting for link in Cloud Mail ({account.email}) ({timeout}s)")
