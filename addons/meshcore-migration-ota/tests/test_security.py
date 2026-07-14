from __future__ import annotations

import pytest

from app.server import AddonOptions, create_app

pytestmark = pytest.mark.asyncio


async def test_status_requires_authenticated_ingress_user(
    aiohttp_client, tmp_path
) -> None:
    options = AddonOptions(
        meshtastic_host="127.0.0.1",
        meshcore_host="127.0.0.1",
        meshcore_port=5000,
        data_dir=tmp_path,
        backup_dir=tmp_path,
    )
    client = await aiohttp_client(create_app(options))

    response = await client.get("/api/status")

    assert response.status == 403


async def test_post_requires_csrf_token(aiohttp_client, tmp_path) -> None:
    options = AddonOptions(
        meshtastic_host="127.0.0.1",
        meshcore_host="127.0.0.1",
        meshcore_port=5000,
        data_dir=tmp_path,
        backup_dir=tmp_path,
    )
    client = await aiohttp_client(create_app(options))
    headers = {"X-Remote-User-Id": "admin"}
    await client.get("/api/status", headers=headers)

    response = await client.post("/api/rescan", json={}, headers=headers)

    assert response.status == 403


async def test_ingress_base_path_is_normalized(aiohttp_client, tmp_path) -> None:
    options = AddonOptions(
        meshtastic_host="127.0.0.1",
        meshcore_host="127.0.0.1",
        meshcore_port=5000,
        data_dir=tmp_path,
        backup_dir=tmp_path,
    )
    client = await aiohttp_client(create_app(options))

    response = await client.get(
        "/",
        headers={
            "X-Remote-User-Id": "admin",
            "X-Ingress-Path": "/api/hassio_ingress/token",
        },
    )

    assert response.status == 200
    assert '<base href="/api/hassio_ingress/token/">' in await response.text()
