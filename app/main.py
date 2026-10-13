import logging

from fastapi import FastAPI, HTTPException, Query, Request, Response, status

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("whatsapp_webhook")


app = FastAPI(
    title="WhatsApp Business AI Agent Webhook",
    version="1.0.0",
)


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    return {"status": "ok", "service": "whatsapp-ai-agent"}


@app.get("/webhook", status_code=status.HTTP_200_OK)
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    WhatsApp Cloud API Webhook Verification.
    Meta issues a GET request with hub.mode, hub.verify_token, and hub.challenge.
    """
    if not hub_mode or not hub_verify_token:
        logger.warning("Webhook verification attempt missing parameters.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing verification parameters."
        )

    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("Webhook successfully verified by Meta.")
        # Return hub.challenge as plain text
        return Response(content=hub_challenge, media_type="text/plain")

    logger.warning("Verification failed: tokens do not match.")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Verification token mismatch."
    )


@app.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_webhook_payload(request: Request):
    """
    Initial receiver for incoming webhook events from WhatsApp.
    Full message parsing and routing will be implemented in Stage 2.
    """
    payload = await request.json()
    logger.info("Received WhatsApp event: %s", payload)
    return {"status": "received"}