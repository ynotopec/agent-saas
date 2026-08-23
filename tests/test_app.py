"""
Tests for agents-saas application.
Run: python3 -m pytest tests/test_app.py -v
"""
import pytest
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as app_module
from app import app, build_config, validate_subdomain

AUTH_HEADERS = {"X-Deploy-Token": "test-deploy-token"}


@pytest.fixture
def client(monkeypatch):
    """Test client with test config."""
    monkeypatch.setattr(app_module, "DEPLOY_TOKEN", "test-deploy-token")
    monkeypatch.setattr(app_module, "HERMES_WEBUI_PASSWORD", "test-webui-password")
    monkeypatch.setattr(app_module, "API_SERVER_KEY", "test-api-server-key")
    monkeypatch.setattr(app_module, "LLM_API_KEY", "test-llm-key")
    monkeypatch.setattr(app_module, "LLM_PROVIDER", "test-provider")
    monkeypatch.setattr(app_module, "LLM_MODEL", "test-model")
    monkeypatch.setattr(app_module, "LLM_BASE_URL", "https://test.example.com/v1")
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        yield client


class TestValidateSubdomain:
    """Tests for subdomain validation."""

    def test_valid_subdomain(self, client):
        """Subdomain should accept valid alphanumeric strings."""
        assert validate_subdomain("test") == "test"
        assert validate_subdomain("my-agent") == "my-agent"
        assert validate_subdomain("agent123") == "agent123"

    def test_uppercase_rejected(self, client):
        """Subdomain should reject uppercase letters."""
        with pytest.raises(ValueError):
            validate_subdomain("Test")

    def test_consecutive_hyphens_rejected(self, client):
        """Subdomain should reject consecutive hyphens."""
        with pytest.raises(ValueError):
            validate_subdomain("test--agent")

    def test_invalid_special_chars(self, client):
        """Subdomain should reject special characters."""
        with pytest.raises(ValueError):
            validate_subdomain("test@agent")

    def test_empty_string(self, client):
        """Empty string should be rejected."""
        with pytest.raises(ValueError):
            validate_subdomain("")


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    def test_health_returns_ok(self, client):
        """GET /health should return status ok."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestInstancesEndpoint:
    """Tests for /api/instances endpoint."""

    def test_instances_returns_list(self, client):
        """GET /api/instances should return a list (may be empty without K8s)."""
        response = client.get("/api/instances", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestDeployEndpoint:
    """Tests for /api/deploy endpoint."""

    def test_deploy_requires_authentication(self, client):
        """POST /api/deploy should reject requests without the shared token."""
        response = client.post("/api/deploy", json={"subdomain": "test"})
        assert response.status_code == 401

    @patch('app.k8s_post')
    def test_deploy_returns_success(self, mock_k8s_post, client):
        """POST /api/deploy should return success response when K8s calls succeed."""
        response = client.post("/api/deploy", headers=AUTH_HEADERS, json={"subdomain": "test"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["subdomain"] == "test"
        assert "hash" in data
        assert "url" in data
        assert data["url"] == "https://test.ailab.infocepo.com"

    @patch('app.k8s_post')
    def test_deploy_returns_correct_name(self, mock_k8s_post, client):
        """Deploy should return name with format agent-<hash>-<subdomain>."""
        response = client.post("/api/deploy", headers=AUTH_HEADERS, json={"subdomain": "myapp"})
        assert response.status_code == 200
        data = response.json()
        assert data["name"].startswith("agent-")
        assert data["name"].endswith("-myapp")

    def test_deploy_invalid_subdomain(self, client):
        """POST /api/deploy with invalid subdomain should return 400."""
        response = client.post("/api/deploy", headers=AUTH_HEADERS, json={"subdomain": "INVALID"})
        assert response.status_code == 400


class TestConfigGeneration:
    """Tests for config.yaml generation."""

    def test_config_uses_env_vars(self):
        """Config should use environment variables, not hardcoded globals."""
        with patch.dict(os.environ, {
            "LLM_BASE_URL": "https://test.example.com/v1",
            "LLM_API_KEY": "test-key",
            "LLM_PROVIDER": "test-provider",
            "LLM_MODEL": "test-model",
        }):
            config = build_config()
            assert "test-model" in config
            assert "test-provider" in config
            assert "https://test.example.com/v1" in config
            assert "test-key" in config

    def test_config_contains_required_sections(self):
        """Config should contain all required sections."""
        config = build_config()
        assert "model:" in config
        assert "providers:" in config
        assert "custom_providers:" in config
        assert "toolsets:" in config


class TestChangePasswordEndpoint:
    """Tests for the POST /api/change-password endpoint."""

    @patch("app.k8s_get")
    @patch("app.k8s_post")
    def test_change_password_creates_job(self, mock_k8s_post, mock_k8s_get, client):
        """POST /api/change-password should return success when called with valid data."""
        mock_k8s_get.return_value = {
            "items": [{"metadata": {"name": "agent-abc-test", "labels": {"app": "agent-instance"}}}]
        }
        response = client.post(
            "/api/change-password",
            headers=AUTH_HEADERS,
            json={"subdomain": "test", "new_password": "newsecret123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["subdomain"] == "test"

    @patch("app.k8s_post")
    def test_change_password_rejects_empty_password(self, mock_k8s_post, client):
        """POST /api/change-password with empty password should return 400."""
        response = client.post(
            "/api/change-password",
            headers=AUTH_HEADERS,
            json={"subdomain": "test", "new_password": ""},
        )
        assert response.status_code == 400

    @patch("app.k8s_post")
    def test_change_password_rejects_short_password(self, mock_k8s_post, client):
        """POST /api/change-password with password shorter than 4 chars should return 400."""
        response = client.post(
            "/api/change-password",
            headers=AUTH_HEADERS,
            json={"subdomain": "test", "new_password": "abc"},
        )
        assert response.status_code == 400

    @patch("app.k8s_post")
    def test_change_password_rejects_invalid_subdomain(self, mock_k8s_post, client):
        """POST /api/change-password with invalid subdomain should return 400."""
        response = client.post(
            "/api/change-password",
            headers=AUTH_HEADERS,
            json={"subdomain": "INVALID", "new_password": "newsecret"},
        )
        assert response.status_code == 400


class TestWebExtractEndpoint:
    """Tests for the POST /api/web-extract endpoint."""

    def test_web_extract_empty_urls(self, client):
        """Web extract with empty URL list should return empty results."""
        response = client.post(
            "/api/web-extract",
            json={"urls": []},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"] == []

    @patch("httpx.Client")
    @patch("trafilatura.extract", return_value="This is test content for extraction. More lines to make it substantial enough.")
    def test_web_extract_returns_content_on_success(self, mock_extract, mock_client_class, client):
        """Web extract should return extracted content for successful URLs."""
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.text = "<html><body><p>This is test content for extraction. More lines to make it substantial enough.</p></body></html>"
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_class.return_value = mock_client

        response = client.post(
            "/api/web-extract",
            json={"urls": ["https://example.com/test"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["error"] is None
        assert "test content" in data["results"][0]["content"]
        assert data["results"][0]["metadata"]["extractor"] == "trafilatura-inline"

    @patch("httpx.Client")
    def test_web_extract_handles_failing_url(self, mock_client_class, client):
        """Web extract should return error for URLs that fail."""
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("Connection refused")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_class.return_value = mock_client

        response = client.post(
            "/api/web-extract",
            json={"urls": ["https://invalid.example.com/nonexistent"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["error"] is not None

    @patch("httpx.Client")
    @patch("trafilatura.extract", return_value="Test content.")
    def test_web_extract_multiple_urls(self, mock_extract, mock_client_class, client):
        """Web extract should handle multiple URLs in one request."""
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.text = "<html><body><p>Test content.</p></body></html>"
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_class.return_value = mock_client

        response = client.post(
            "/api/web-extract",
            json={"urls": ["https://example.com/1", "https://example.com/2"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 2
        for r in data["results"]:
            assert r["error"] is None

    @patch("httpx.Client")
    def test_web_extract_prefers_http_service_when_configured(self, mock_client_class, client):
        """When TRAFILATURA_LOCAL_URL is set, HTTP service should be tried first."""
        from unittest.mock import MagicMock

        # The mock_client is for the HTTP service call
        mock_service_response = MagicMock()
        mock_service_response.raise_for_status = MagicMock()
        mock_service_response.json.return_value = {
            "results": [{"url": "https://example.com", "content": "from-service", "error": None}]
        }
        mock_service_client = MagicMock()
        mock_service_client.post.return_value = mock_service_response
        mock_service_client.__enter__ = MagicMock(return_value=mock_service_client)
        mock_service_client.__exit__ = MagicMock(return_value=False)

        mock_http_client = MagicMock()
        mock_http_client.get.return_value = MagicMock()
        mock_http_client.__enter__ = MagicMock(return_value=mock_http_client)
        mock_http_client.__exit__ = MagicMock(return_value=False)

        # First call is the HTTP service (POST /extract), second is inline fallback (GET url)
        side_effects = [mock_service_client, mock_http_client]
        call_index = [0]

        def get_side_effect(*args, **kwargs):
            idx = call_index[0]
            call_index[0] += 1
            return side_effects[idx] if idx < len(side_effects) else MagicMock()

        mock_client_class.side_effect = get_side_effect

        with patch.dict(os.environ, {"TRAFILATURA_LOCAL_URL": "http://localhost:8990"}):
            with patch("os.environ.get", side_effect=lambda k, d="": {
                "TRAFILATURA_LOCAL_URL": "http://localhost:8990",
            }.get(k, d)):
                response = client.post(
                    "/api/web-extract",
                    json={"urls": ["https://example.com"]},
                )
                assert response.status_code == 200
                data = response.json()
                assert len(data["results"]) == 1
                assert data["results"][0]["error"] is None
                assert data["results"][0]["metadata"]["extractor"] == "trafilatura-local-http"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
