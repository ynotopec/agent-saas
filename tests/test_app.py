"""
Tests for agents-saas application.
Run: python3 -m pytest tests/test_app.py -v
"""
import pytest
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import app, validate_subdomain, build_config, list_instances, deploy_instance


@pytest.fixture
def client():
    """Test client with test config."""
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
        response = client.get("/api/instances")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestDeployEndpoint:
    """Tests for /api/deploy endpoint."""

    @patch('app.k8s_post')
    def test_deploy_returns_success(self, mock_k8s_post, client):
        """POST /api/deploy should return success response when K8s calls succeed."""
        response = client.post("/api/deploy", json={"subdomain": "test"})
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
        response = client.post("/api/deploy", json={"subdomain": "myapp"})
        assert response.status_code == 200
        data = response.json()
        assert data["name"].startswith("agent-")
        assert data["name"].endswith("-myapp")

    def test_deploy_invalid_subdomain(self, client):
        """POST /api/deploy with invalid subdomain should return 400."""
        response = client.post("/api/deploy", json={"subdomain": "INVALID"})
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
