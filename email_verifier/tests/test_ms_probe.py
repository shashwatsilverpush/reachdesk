"""Tests for the Microsoft 365 account existence probe."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from email_verifier.ms_probe import check_m365_account, is_m365_mx


class TestIsM365Mx:
    def test_standard_mx(self):
        assert is_m365_mx("contoso-com.mail.protection.outlook.com") is True

    def test_with_trailing_dot(self):
        assert is_m365_mx("contoso-com.mail.protection.outlook.com.") is True

    def test_eo_variant(self):
        assert is_m365_mx("contoso-com.mail.eo.outlook.com") is True

    def test_case_insensitive(self):
        assert is_m365_mx("Contoso-com.Mail.Protection.Outlook.com") is True

    def test_non_m365_mx(self):
        assert is_m365_mx("alt1.gmail-smtp-in.l.google.com") is False

    def test_generic_mx(self):
        assert is_m365_mx("mx.example.com") is False


class TestCheckM365Account:
    def _mock_response(self, if_exists: int, throttle: int = 0, status_code: int = 200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = {
            "IfExistsResult": if_exists,
            "ThrottleStatus": throttle,
        }
        return mock_resp

    @patch("email_verifier.ms_probe.httpx.Client")
    def test_account_exists(self, mock_client_cls):
        ctx = MagicMock()
        ctx.post.return_value = self._mock_response(0)
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        assert check_m365_account("user@contoso.com") is True

    @patch("email_verifier.ms_probe.httpx.Client")
    def test_account_not_found(self, mock_client_cls):
        ctx = MagicMock()
        ctx.post.return_value = self._mock_response(1)
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        assert check_m365_account("fake@contoso.com") is False

    @patch("email_verifier.ms_probe.httpx.Client")
    def test_federated_exists(self, mock_client_cls):
        ctx = MagicMock()
        ctx.post.return_value = self._mock_response(5)
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        assert check_m365_account("user@federated.com") is True

    @patch("email_verifier.ms_probe.httpx.Client")
    def test_domain_hint_exists(self, mock_client_cls):
        ctx = MagicMock()
        ctx.post.return_value = self._mock_response(6)
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        assert check_m365_account("user@corp.com") is True

    @patch("email_verifier.ms_probe.httpx.Client")
    def test_throttled_returns_none(self, mock_client_cls):
        ctx = MagicMock()
        ctx.post.return_value = self._mock_response(0, throttle=1)
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        assert check_m365_account("user@contoso.com") is None

    @patch("email_verifier.ms_probe.httpx.Client")
    def test_http_error_returns_none(self, mock_client_cls):
        ctx = MagicMock()
        ctx.post.return_value = self._mock_response(0, status_code=500)
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        assert check_m365_account("user@contoso.com") is None

    @patch("email_verifier.ms_probe.httpx.Client")
    def test_network_error_returns_none(self, mock_client_cls):
        ctx = MagicMock()
        ctx.post.side_effect = Exception("connection refused")
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        assert check_m365_account("user@contoso.com") is None
