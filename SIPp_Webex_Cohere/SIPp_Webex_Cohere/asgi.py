# SIPp_Webex_Cohere/asgi.py
import os
import json
import httpx
from pathlib import Path
from django.conf import settings
from urllib.parse import urlparse
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "SIPp_Webex_Cohere.settings")

django_app = get_asgi_application()
CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "Webex_Cohere_Agent" / "config.json"
)


async def _get_ngrok_url() -> str | None:
    """Query ngrok's local API for the current public HTTPS tunnel URL."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://127.0.0.1:4040/api/tunnels", timeout=5)
            for tunnel in resp.json().get("tunnels", []):
                if tunnel["proto"] == "https":
                    return tunnel["public_url"]
    except Exception:
        pass
    return None


async def _register_webhook(ngrok_url: str) -> None:
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    token = cfg["WEBEX_BOT_TOKEN"]
    target = ngrok_url.rstrip("/") + "/chat_webhook/"

    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://webexapis.com/v1/webhooks",
            headers={"Authorization": f"Bearer {token}"},
        )
        for wh in r.json().get("items", []):
            await client.delete(
                f"https://webexapis.com/v1/webhooks/{wh['id']}",
                headers={"Authorization": f"Bearer {token}"},
            )

        r = await client.post(
            "https://webexapis.com/v1/webhooks",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "name": "SIPp Agent Webhook",
                "targetUrl": target,
                "resource": "messages",
                "event": "created",
            },
        )
        if r.status_code == 200:
            print(f"[startup] Webhook registered - {target}")
        else:
            print(f"[startup] Webhook registration failed: {r.status_code} {r.text}")


async def application(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                url = await _get_ngrok_url()
                if url:
                    # Inject ngrok hostname into Django's ALLOWED_HOSTS at runtime

                    ngrok_host = urlparse(url).netloc
                    if ngrok_host not in settings.ALLOWED_HOSTS:
                        settings.ALLOWED_HOSTS.append(ngrok_host)

                    await _register_webhook(url)
                else:
                    print(
                        "[startup] ngrok not found - start ngrok first, then restart Django"
                    )
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    else:
        await django_app(scope, receive, send)
