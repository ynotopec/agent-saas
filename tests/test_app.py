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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
