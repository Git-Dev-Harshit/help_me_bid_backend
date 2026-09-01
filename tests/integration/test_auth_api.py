"""Integration tests for registration, login and the authenticated surface."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"


async def register(client, phone="9876543210", password="Str0ngPass1", **extra):
    return await client.post(
        REGISTER, json={"phone_number": phone, "password": password, **extra}
    )


async def login_token(client, phone="9876543210", password="Str0ngPass1") -> str:
    response = await client.post(LOGIN, json={"phone_number": phone, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


class TestRegistration:
    async def test_creates_an_account(self, api_client):
        response = await register(api_client, name="Asha Menon", email="Asha@Example.com")
        assert response.status_code == 201
        body = response.json()
        assert body["phone_number"] == "+919876543210"
        assert body["email"] == "asha@example.com"  # lower-cased
        assert body["is_active"] is True
        assert "password" not in body and "hashed_password" not in body

    @pytest.mark.parametrize(
        "variant", ["9876543210", "+919876543210", "+91 98765 43210", "098765 43210"]
    )
    async def test_rejects_the_same_number_in_any_format(self, api_client, variant):
        assert (await register(api_client)).status_code == 201
        duplicate = await register(api_client, phone=variant)
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "PHONE_ALREADY_REGISTERED"

    @pytest.mark.parametrize("password", ["short1", "alllettersonly", "12345678"])
    async def test_rejects_weak_passwords(self, api_client, password):
        response = await register(api_client, password=password)
        assert response.status_code == 422

    @pytest.mark.parametrize("phone", ["abc", "12", ""])
    async def test_rejects_invalid_phone_numbers(self, api_client, phone):
        assert (await register(api_client, phone=phone)).status_code == 422

    async def test_phone_number_is_mandatory(self, api_client):
        response = await api_client.post(REGISTER, json={"password": "Str0ngPass1"})
        assert response.status_code == 422


class TestLogin:
    async def test_returns_a_usable_token(self, api_client):
        await register(api_client)
        response = await api_client.post(
            LOGIN, json={"phone_number": "9876543210", "password": "Str0ngPass1"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["access_token"]

    async def test_accepts_any_equivalent_phone_format(self, api_client):
        await register(api_client)
        response = await api_client.post(
            LOGIN, json={"phone_number": "+91 98765 43210", "password": "Str0ngPass1"}
        )
        assert response.status_code == 200

    async def test_wrong_password_and_unknown_account_are_indistinguishable(self, api_client):
        """The endpoint must not reveal which phone numbers are registered."""
        await register(api_client)
        wrong = await api_client.post(
            LOGIN, json={"phone_number": "9876543210", "password": "Wr0ngPass1"}
        )
        unknown = await api_client.post(
            LOGIN, json={"phone_number": "9000000009", "password": "Wr0ngPass1"}
        )
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()


class TestProtectedEndpoints:
    async def test_requires_a_token(self, api_client):
        response = await api_client.get("/api/v1/users/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_TOKEN"

    async def test_rejects_a_malformed_token(self, api_client):
        response = await api_client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer nonsense"}
        )
        assert response.status_code == 401

    async def test_returns_the_profile(self, api_client):
        await register(api_client, name="Asha Menon")
        token = await login_token(api_client)
        response = await api_client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Asha Menon"
        assert response.json()["last_login_at"] is not None

    async def test_updates_the_profile(self, api_client):
        await register(api_client)
        token = await login_token(api_client)
        response = await api_client.patch(
            "/api/v1/users/me",
            json={"name": "Updated Name"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Updated Name"


class TestErrorEnvelope:
    async def test_every_failure_uses_the_documented_shape(self, api_client):
        response = await api_client.get("/api/v1/ipos/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
        body = response.json()
        assert body["success"] is False
        assert body["error"]["code"] == "IPO_NOT_FOUND"
        assert isinstance(body["error"]["message"], str)

    async def test_internal_details_are_never_leaked(self, api_client):
        response = await api_client.get("/api/v1/ipos?sort_by=not_a_column")
        assert response.status_code == 422
        assert "Traceback" not in response.text
        assert "sqlalchemy" not in response.text.lower()
