"""Tests for CloudMailbox provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from core.base_mailbox import MailboxAccount, create_mailbox
from core.cloud_mail import CloudMailbox, _generate_random_email


def test_generate_random_email():
    email = _generate_random_email("testdomain.com")
    assert email.endswith("@testdomain.com")
    assert email.startswith("usr_")


def test_cloud_mailbox_get_email_random_mode():
    box = CloudMailbox(
        api_url="https://mail.example.com",
        api_token="test-token-123",
        domain="example.com",
        mode="random",
    )
    account = box.get_email()
    assert "@example.com" in account.email
    assert account.extra["provider_account"]["provider_name"] == "cloud_mail"


@patch("requests.Session.post")
def test_cloud_mailbox_get_email_api_create_mode(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"email": "created_user@example.com", "id": "12345"}
    mock_post.return_value = mock_resp

    box = CloudMailbox(
        api_url="https://mail.example.com",
        api_token="test-token",
        domain="example.com",
        mode="api_create",
    )
    account = box.get_email()
    assert account.email == "created_user@example.com"
    assert account.account_id == "12345"


@patch("requests.Session.get")
def test_cloud_mailbox_wait_for_code(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {
            "id": "msg-101",
            "subject": "Your verification code is 884920",
            "body": "Welcome! Enter 884920 to verify your email.",
        }
    ]
    mock_get.return_value = mock_resp

    box = CloudMailbox(
        api_url="https://mail.example.com",
        api_token="token123",
        domain="example.com",
        poll_interval=0.1,
    )
    account = MailboxAccount(email="test@example.com", account_id="test@example.com")
    code = box.wait_for_code(account, keyword="verification", timeout=2)
    assert code == "884920"


def test_cloud_mailbox_peek_email():
    box = CloudMailbox(
        api_url="https://mail.example.com",
        domain="mydomain.org",
    )
    assert box.peek_email() == "preview_test@mydomain.org"
