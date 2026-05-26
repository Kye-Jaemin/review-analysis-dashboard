import pytest


@pytest.mark.asyncio
async def test_category_crud(app_client):
    # Create root
    r = await app_client.post("/categories", json={"name": "UX", "description": "User experience"})
    assert r.status_code == 200
    ux_id = r.json()["id"]

    # Create child
    r = await app_client.post("/categories", json={"name": "Onboarding", "description": "First-time UX", "parent_id": ux_id})
    assert r.status_code == 200
    onboarding_id = r.json()["id"]
    assert r.json()["path"] == "UX > Onboarding"

    # List
    r = await app_client.get("/api/categories")
    tree = r.json()["tree"]
    assert any(node["name"] == "UX" and node["children"] for node in tree)

    # Patch
    r = await app_client.patch(f"/categories/{onboarding_id}", json={"description": "Updated"})
    assert r.status_code == 200

    # Delete
    r = await app_client.delete(f"/categories/{ux_id}")
    assert r.status_code == 200
