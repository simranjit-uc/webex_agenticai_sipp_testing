from django.shortcuts import render

import json
import re
import requests
import logging
import datetime
import httpx
import asyncio
from pathlib import Path
from asgiref.sync import markcoroutinefunction

import hmac
import hashlib
import base64

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

# ******************************* Config *******************************
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "SIPp-Webex-Bot.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s : %(levelname)s : %(message)s",
)
logger = logging.getLogger(__name__)

with open(CONFIG_PATH, "r") as f:
    _cfg = json.load(f)

WEBEX_BOT_TOKEN = _cfg["WEBEX_BOT_TOKEN"]
BOT_EMAIL = _cfg["BOT_EMAIL"]
# CUBE_IP = _cfg["CUBE_HOST"]
# SBC_IP = _cfg["SBC_IP"]

WEBEX_API_URL = "https://webexapis.com/v1/messages/"
MCP_SERVER_URL = "http://localhost:3333/mcp"


# ******************************* Webex helpers *******************************


def send_webex_msg(room_id: str, text: str):
    """Post a plain text message to a Webex room."""
    headers = {
        "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        requests.post(
            WEBEX_API_URL, headers=headers, json={"roomId": room_id, "text": text}
        )
    except Exception as e:
        logger.error(f"send_webex_msg failed: {e}")


async def send_ack_message(room_id: str) -> str:
    """Send one ack message and return its ID for later editing."""
    headers = {
        "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                WEBEX_API_URL,
                headers=headers,
                json={"roomId": room_id, "text": "Running SIP test. Please wait..."},
            )
            return r.json().get("id", "")
    except Exception as e:
        logger.error(f"send_ack_message failed: {e}")
        return ""


async def update_ack_message(ack_id: str, room_id: str, text: str) -> None:
    """Edit the existing ack message in place."""
    headers = {
        "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.put(
                f"{WEBEX_API_URL}{ack_id}",
                headers=headers,
                json={"roomId": room_id, "markdown": text},
            )
    except Exception as e:
        logger.error(f"update_ack_message failed: {e}")


# ******************************* MCP tool caller *******************************


async def call_mcp_tool(
    client: httpx.AsyncClient, tool_name: str, arguments: dict
) -> dict:
    """
    Call one MCP tool via JSON-RPC and return the structured result."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"session": "default", "name": tool_name, "arguments": arguments},
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    logger.info(f"Calling MCP tool: {tool_name} with args: {arguments}")

    try:
        resp = await client.post(
            MCP_SERVER_URL, json=payload, headers=headers, timeout=60
        )
        logger.info(f"MCP {tool_name} status: {resp.status_code}")

        result = None
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
                structured = data.get("result", {}).get("structuredContent", {})
                # String tools wrap result as {"result": "..."}, dict tools ARE the result
                result = structured.get("result") or (
                    structured if structured else None
                )

        if result is None:
            return {"error": f"No result returned from MCP tool {tool_name}"}

        return result

    except Exception as e:
        logger.error(f"MCP tool {tool_name} failed: {e}")
        return {"error": str(e)}


# ******************************* Message parser *******************************


def parse_extension(message_text: str) -> str:
    """
    Extract a phone number or extension from the user's message.
    Looks for sequences of 4 or more digits.
    """
    match = re.search(r"\b(\d{4,15})\b", message_text)
    return match.group(1) if match else None


def parse_cube_ip(text: str) -> str | None:
    """Extract an IP address from the message text."""
    match = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", text)
    return match.group(1) if match else None


def format_diagnosis(result: dict) -> str:
    """
    Format the Cohere diagnosis dict into a readable Webex message.
    """
    if "error" in result:
        return f"Something went wrong during analysis:\n{result['error']}"

    return (
        f"SIPp Test Result — Extension: {result.get('extension', 'N/A')}\n"
        f"Outcome: {result.get('sipp_outcome', 'N/A')}\n\n"
        f"Summary:\n{result.get('summary', '')}\n\n"
        f"Root Cause:\n{result.get('root_cause', '')}\n\n"
        f"Recommended Action:\n{result.get('recommended_action', '')}"
    )


# ******************************* Webhook *******************************


@markcoroutinefunction
@csrf_exempt
async def chat_webhook(request):
    """
    Webex webhook endpoint. Receives message events, runs the full
    SIPp sequence, and posts the diagnosis back to Webex."""
    
    payload = json.loads(request.body)
    message_id = payload["data"]["id"]
    room_id = payload["data"]["roomId"]

    headers = {"Authorization": f"Bearer {WEBEX_BOT_TOKEN}"}

    # Fetch full message
    try:
        resp = requests.get(f"{WEBEX_API_URL}{message_id}", headers=headers)
        message_data = resp.json()
        message_text = message_data.get("text", "")
        sender_email = message_data.get("personEmail", "")
        logger.info(f"Message from {sender_email}: {message_text}")
    except Exception as e:
        logger.error(f"Failed to fetch Webex message: {e}")
        return JsonResponse({"status": "error"})

    # Ignore bot's own messages
    if sender_email == BOT_EMAIL or sender_email.endswith("@webex.bot"):
        return JsonResponse({"status": "ignored"})

    if not message_text:
        return JsonResponse({"status": "ignored"})

    # Extract extension from the message
    extension = parse_extension(message_text)
    cube_ip = parse_cube_ip(message_text)

    if not extension:
        send_webex_msg(
            room_id,
            "Please include a phone number or extension in your message. Example: test 918765674532",
        )
        return JsonResponse({"status": "ok"})

    if not cube_ip:
        send_webex_msg(
            room_id,
            "Please include the CUBE IP address in your message. Example: test 918765674532 via 10.1.1.1",
        )
        return JsonResponse({"status": "ok"})

    ack_id = await send_ack_message(room_id)

    # ******************************* Run the four MCP tools in sequence *******************************
    async with httpx.AsyncClient() as client:

        # Step 1 - Enable CUBE debug
        logger.info(f"Step 1: enable_debug for {extension}")
        await update_ack_message(ack_id, room_id, "Enabling CUBE debug...")
        result_1 = await call_mcp_tool(client, "enable_debug", {"cube_ip": cube_ip})
        if isinstance(result_1, dict) and "error" in result_1:
            send_webex_msg(
                room_id,
                f"Could not enable CUBE debug. Check CUBE connectivity.\nError: {result_1['error']}",
            )
            return JsonResponse({"status": "error"})

        # Step 2 - Run SIPp test
        logger.info(f"Step 2: run_sipp_test for {extension}")
        await update_ack_message(ack_id, room_id, "Running SIPp test...")
        result_2 = await call_mcp_tool(
            client, "run_sipp_test", {"extension": extension, "target_ip": cube_ip}
        )
        if isinstance(result_2, dict) and "error" in result_2:
            send_webex_msg(
                room_id,
                f"SIPp test failed for {extension}.\nError: {result_2['error']}",
            )
            return JsonResponse({"status": "error"})

        # Extract Call-ID and outcome from the tool response text
        sipp_text = result_2 if isinstance(result_2, str) else str(result_2)
        call_id_match = re.search(r"Call-ID\s*:\s*(\S+)", sipp_text)
        outcome_match = re.search(r"Outcome\s*:\s*(.+)", sipp_text)
        call_id = call_id_match.group(1).strip() if call_id_match else ""
        sipp_outcome = outcome_match.group(1).strip() if outcome_match else "UNKNOWN"

        logger.info(f"SIPp result: outcome={sipp_outcome}, call_id={call_id}")

        # Step 3 - Disable debug and pull logs
        logger.info(f"Step 3: disable_and_pull_logs for {extension}")
        await update_ack_message(ack_id, room_id, "Pulling CUBE logs...")
        result_3 = await call_mcp_tool(
            client,
            "disable_and_pull_logs",
            {"cube_ip": cube_ip, "call_id": call_id, "extension": extension},
        )
        if isinstance(result_3, dict) and "error" in result_3:
            send_webex_msg(
                room_id,
                f"Could not pull CUBE logs for {extension}.\nError: {result_3['error']}",
            )
            return JsonResponse({"status": "error"})

        # Step 4 - Read filtered log and get Cohere diagnosis
        logger.info(f"Step 4: read_filtered_log for {extension}")
        await update_ack_message(ack_id, room_id, "Analysing with LLM...")
        result_4 = await call_mcp_tool(
            client,
            "read_filtered_log",
            {"extension": extension, "sipp_outcome": sipp_outcome},
        )

    # Format and post diagnosis to Webex
    reply = format_diagnosis(result_4)

    if ack_id:
        headers = {
            "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            await client.put(
                f"{WEBEX_API_URL}{ack_id}",
                headers=headers,
                json={"roomId": room_id, "markdown": reply},
            )
    else:
        send_webex_msg(room_id, reply) 

    logger.info(f"Diagnosis posted to Webex for extension {extension}")

    return JsonResponse({"status": "ok"})
