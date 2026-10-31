import os
import re
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

import cohere
import openai
from google import genai

import uvicorn
from fastmcp import FastMCP

from SBC_Tools import enable_debug as cube_enable_debug
from SBC_Tools import disable_and_pull_logs as cube_disable_and_pull_logs

# ************************* Logging ************************
BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "SIPp-MCP-Cohere.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s : %(levelname)s : %(message)s",
)
logger = logging.getLogger(__name__)


# ************************* Config *************************
CONFIG_PATH = BASE_DIR / "config.json"

with open(CONFIG_PATH, "r") as f:
    _cfg = json.load(f)

COHERE_API_KEY = _cfg["COHERE_API_KEY"]
LLM_PROVIDER = _cfg.get("LLM_PROVIDER", "cohere").lower()
LLM_API_KEY = _cfg["LLM_API_KEY"]
CUBE_USER = _cfg["CUBE_USER"]
CUBE_PASS = _cfg["CUBE_PASS"]
SIPP_PATH = _cfg.get("SIPP_PATH", "/usr/bin/sipp")
SIPP_LOCAL_IP = _cfg["SIPP_LOCAL_IP"]


# ************************* Paths *************************
LOG_DIR = BASE_DIR / "test_logs"
SCENARIO_FILE = BASE_DIR / "basic_call.xml"

LOG_DIR.mkdir(exist_ok=True)


# ************************* Cohere client *************************
co = cohere.ClientV2(COHERE_API_KEY)


# ************************* FastMCP - HTTP mode *************************
mcp = FastMCP("sipp_mcp_server_cohere")


# ************************* Helpers *************************
def _extract_call_id(msg_log_path: str) -> str:
    """Extract Call-ID value from SIPp message log."""
    if not os.path.exists(msg_log_path):
        return ""
    with open(msg_log_path, "r") as f:
        content = f.read()
    match = re.search(r"Call-ID:\s*(\S+)", content, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _get_outcome(msg_log_path: str) -> str:
    """Determine call outcome from SIP response codes in SIPp message log."""
    if not os.path.exists(msg_log_path):
        return "UNKNOWN"
    with open(msg_log_path, "r") as f:
        content = f.read()
    responses = re.findall(r"SIP/2\.0 (\d{3})", content)
    if "200" in responses:
        return "ANSWERED"
    if "486" in responses:
        return "BUSY"
    if "404" in responses:
        return "NOT FOUND"
    if "403" in responses:
        return "REJECTED"
    if "407" in responses:
        return "AUTH REQUIRED"
    if "500" in responses:
        return "SERVER ERROR"
    return "UNKNOWN"


def _filter_log_by_call_id(raw_log: str, call_id: str, context: int = 5) -> str:
    """Filter raw CUBE log to lines matching call_id with surrounding context."""
    if not call_id:
        return raw_log
    lines = raw_log.splitlines()
    matched_indices = set()
    for i, line in enumerate(lines):
        if call_id in line:
            start = max(0, i - context)
            end = min(len(lines), i + context + 1)
            matched_indices.update(range(start, end))
    if not matched_indices:
        return raw_log
    return "\n".join(lines[i] for i in sorted(matched_indices))


def _analyse_with_llm(filtered_log: str, extension: str, sipp_outcome: str) -> dict:
    """
    Send filtered CUBE debug log to the configured LLM and return a diagnosis.
    Provider is set via LLM_PROVIDER in config.json
    """
    prompt = f"""You are a senior VoIP and SIP network engineer specialising in Cisco CUBE and PSTN call routing.

A SIPp test call was placed to the following number: {extension}
SIPp reported the following outcome: {sipp_outcome}

Below is the filtered CUBE debug log captured during the test call.
Note: A CANCEL message appearing after a 183 Session Progress or 200 OK is expected and intentional. 
The SIPp scenario sends a CANCEL after a 10 second pause to terminate the call
cleanly. Do NOT treat this as a failure or warning.

Analyse the log and write three things:

1. A plain English summary of what happened on this call - what was sent,
   what came back and whether the call succeeded or failed.

2. Root cause - if the call failed, explain exactly why based on the SIP
   response codes or error messages visible in the log. If the call succeeded,
   state that clearly.

3. Recommended next steps - what the engineer should do based on this result.
   If the call succeeded, say so and suggest no action is needed unless
   something looks unusual.

Separate all three sections with a line that says exactly: ---DIV---
Do not add any other headers, labels or preamble before any section.

--- FILTERED CUBE DEBUG LOG ---
{filtered_log}
--- END OF LOG ---
"""

    try:
        if LLM_PROVIDER == "cohere":
            co = cohere.ClientV2(LLM_API_KEY)
            response = co.chat(
                model="command-a-03-2025",
                messages=[{"role": "user", "content": prompt}],
            )
            result_text = response.message.content[0].text
        else:
            return {"error": f"Unknown LLM_PROVIDER: {LLM_PROVIDER}"}

        summary, root_cause, recommended_action = result_text.split("---DIV---")  

        logger.info(
            f"LLM ({LLM_PROVIDER}) analysis complete for {extension}. Outcome: {sipp_outcome}"
        )

        return {
            "extension": extension,
            "sipp_outcome": sipp_outcome,
            "summary": summary.strip(),
            "root_cause": root_cause.strip(),
            "recommended_action": recommended_action.strip(),
        }

    except Exception as e:
        logger.error(f"LLM analysis failed for {extension}: {e}")
        return {"error": f"LLM analysis failed: {e}"}


# --------------------------------------------------------
# MCP TOOLS
# --------------------------------------------------------

@mcp.tool()
def enable_debug(cube_ip: str) -> str:
    """
    SSH into the CUBE router and enable 'debug ccsip messages'.
    Always call this FIRST before running a SIPp test.

    Args:
        cube_ip: IP address of the CUBE router.
    """

    cube_cfg = {
        "device_type": "cisco_ios",
        "host": cube_ip,
        "username": CUBE_USER,
        "password": CUBE_PASS,
        "timeout": 30,
    }

    logger.info(f"enable_debug called for CUBE: {cube_ip}")
    success = cube_enable_debug(cube_config=cube_cfg)
    if success:
        return (
            f"debug ccsip messages enabled on CUBE {cube_ip}. Ready to run SIPp test."
        )
    return (
        f"Failed to enable debug on CUBE {cube_ip}. Check connectivity and credentials."
    )


@mcp.tool()
def run_sipp_test(extension: str, target_ip: str) -> str:
    """
    Run a single SIPp test call to a phone number or extension.
    Returns the call outcome and Call-ID needed for log filtering.
    Always call this AFTER enable_debug and BEFORE disable_and_pull_logs.

    Args:
        extension: The phone number or extension to test.
        target_ip: IP address of the SBC or SIP server.
    """
    logger.info(f"run_sipp_test: extension={extension}, target={target_ip}")

    safe_ext = extension.replace("/", "_")
    msg_log = str(LOG_DIR / f"{safe_ext}_messages.log")
    err_log = str(LOG_DIR / f"{safe_ext}_errors.log")
    stats_file = str(LOG_DIR / f"{safe_ext}_stats.csv")

    cmd = [
        SIPP_PATH,
        target_ip,
        "-sf",
        str(SCENARIO_FILE),
        "-s",
        extension,
        "-i",
        SIPP_LOCAL_IP,
        "-m",
        "1",
        "-d",
        "15000",
        "-timeout",
        "30",
        "-trace_msg",
        "-message_file",
        msg_log,
        "-trace_err",
        "-error_file",
        err_log,
        "-trace_stat",
        "-stf",
        stats_file,
        "-nostdin",
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=45, cwd=str(LOG_DIR)
        )
    except subprocess.TimeoutExpired:
        logger.error(f"SIPp timed out for extension {extension}")
        return f"SIPp process timed out for extension {extension}."

    call_id = _extract_call_id(msg_log)
    outcome = _get_outcome(msg_log)

    logger.info(
        f"SIPp result: extension={extension}, outcome={outcome}, call_id={call_id}"
    )

    return (
        f"SIPp test complete.\n"
        f"Extension  : {extension}\n"
        f"Outcome    : {outcome}\n"
        f"Call-ID    : {call_id}\n"
        f"Return code: {proc.returncode}\n\n"
        f"Pass Call-ID '{call_id}' and outcome '{outcome}' to disable_and_pull_logs."
    )


@mcp.tool()
def disable_and_pull_logs(cube_ip: str, call_id: str, extension: str) -> str:
    """
    SSH into the CUBE router, disable debug ccsip messages, pull the full
    log buffer, filter to the Call-ID for this test call and save both
    raw and filtered logs to disk.
    Always call this AFTER run_sipp_test.

    Args:
        cube_ip:   IP address of the CUBE router.
        call_id:   The Call-ID returned by run_sipp_test.
        extension: The extension that was tested — used to name the log files.
    """
    cube_cfg = {
        "device_type": "cisco_ios",
        "host": cube_ip,
        "username": CUBE_USER,
        "password": CUBE_PASS,
        "timeout": 30,
    }

    logger.info(
        f"disable_and_pull_logs: cube={cube_ip}, call_id={call_id}, extension={extension}"
    )

    raw_log = cube_disable_and_pull_logs(cube_config=cube_cfg)

    if not raw_log:
        return "No log output retrieved from CUBE. Cannot proceed with analysis."

    # Save full raw log with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = LOG_DIR / f"cube_raw_{extension}_{timestamp}.log"
    raw_path.write_text(raw_log)

    # Filter by Call-ID and save
    filtered = _filter_log_by_call_id(raw_log, call_id)
    safe_ext = extension.replace("/", "_")
    filtered_path = LOG_DIR / f"cube_filtered_{safe_ext}.log"
    filtered_path.write_text(filtered)

    total_lines = len(raw_log.splitlines())
    filtered_lines = len(filtered.splitlines())

    logger.info(
        f"Log saved: {total_lines} raw lines, {filtered_lines} filtered lines for {extension}"
    )

    return (
        f"Debug disabled on CUBE {cube_ip}.\n"
        f"Raw log  : {total_lines} lines saved.\n"
        f"Filtered : {filtered_lines} lines matched Call-ID '{call_id}'.\n\n"
        f"Call read_filtered_log with extension '{extension}' and the sipp_outcome to get the diagnosis."
    )


@mcp.tool()
def read_filtered_log(extension: str, sipp_outcome: str) -> dict:
    """
    Read the filtered CUBE debug log for a previously tested extension,
    send it to Cohere and return a finished plain English diagnosis.

    Returns a dict with summary, root_cause and recommended_action.
    A CANCEL message appearing after a 183 Session Progress or 200 OK is expected and intentional.
    The SIPp scenario sends a CANCEL after a 10 second pause to terminate the call cleanly

    Args:
        extension:    The extension that was tested.
        sipp_outcome: The call outcome from run_sipp_test e.g. ANSWERED, BUSY.
    """
    safe_ext = extension.replace("/", "_")
    filtered_path = LOG_DIR / f"cube_filtered_{safe_ext}.log"

    if not filtered_path.exists():
        return {
            "error": f"Filtered log not found for extension {extension}. Run disable_and_pull_logs first."
        }

    filtered_log = filtered_path.read_text()

    if not filtered_log.strip():
        return {
            "error": f"Filtered log is empty for extension {extension}. Call-ID may not have matched any CUBE log lines."
        }

    return _analyse_with_llm(filtered_log, extension, sipp_outcome)


# -------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------

app = mcp.http_app(stateless_http=True)

if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=3333, log_level="info")
