# Cisco Webex Bot - Agentic AI VoIP Call Testing with Python, MCP and Cohere LLM

Trigger a SIP call test from a Webex message and get back an AI-generated diagnosis including a plain-English root cause analysis.

This repository accompanies the multi-part blog series on [learnuccollab.com](https://learnuccollab.com/category/sipp/) and video playlist on my [YouTube](https://www.youtube.com/@learnuccollab).

![Python](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python)
![Django](https://img.shields.io/badge/Django-Async-green?style=flat-square&logo=django)
![Cohere](https://img.shields.io/badge/LLM-Cohere-orange?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-lightgrey?style=flat-square)

---

## What it does

Send a message to the bot in a Webex space. For example:

```
@SIPpBot test 911234567890 on 10.1.1.2
```

The bot will:

1. Enable `debug ccsip messages` on the CUBE (e.g 10.1.1.2) via SSH
2. Run a SIPp UAC test call to the extension
3. Disable debug and pull the full log buffer from the CUBE
4. Filter the logs by Call-ID and send them to Cohere for LLM analysis
5. Reply with a structured **Summary**, **Root Cause**, and **Recommended Action**

---

## Architecture

```
Webex User
    │
    │  (mention bot or chat 1:1)
    ▼
Webex Cloud -- ngrok tunnel --► Django (chat_webhook)
                                        │
                                  asyncio pipeline
                                        │
                      ┌─────────────────┼─────────────────┐
                      ▼                 ▼                  ▼
               enable_debug       run_sipp_test    disable_and_pull_logs
             (SSH → CUBE)         (SIPp UAC)       (SSH → CUBE)
                                        │
                                        ▼
                                read_filtered_log
                                        │
                                        ▼
                                    Cohere LLM
                                        │
                                        ▼
                                   Webex Reply
```

All four tools run as **MCP tools** served by a local MCP server over StreamableHTTP on `localhost:3333`.
The Django webhook view is **async** and must be run under **uvicorn**.

---

## Requirements

- Python 3.11+
- Cisco CUBE reachable from the SIPp host via SSH
- SIPp installed and available on the system `PATH`
- [Cohere API key](https://cohere.com)
- Webex Bot token
- Django Framework

### Python dependencies

```bash
pip install fastmcp uvicorn netmiko httpx django
```

### Create Django Project

```bash
django-admin startproject SIPp_Webex_Cohere
```

### Create Django App within the project

```bash
cd SIPp_Webex_Cohere
python manage.py startapp Webex_Cohere_Agent
```

### Copy & Paste files
- Download and copy SBC_Tools.py, SIPp_MCP_Svr_Cohere_v1.py, basic_call.xml, config.json, urls.py and views.py to the newly created **Webex_Cohere_Agent** Django app opn your machine
- Download and copy asgi.py and urls.py to the newly created **SIPp_Webex_Cohere** project folder

---

## Final Project Structure

This is how your directory structure should look like

```
SIPp_Webex_Cohere/                          # Django project
├── SIPp_Webex_Cohere
│   ├── urls.py
│   └── asgi.py                             # Registers webhook at startup
|
├── Webex_Cohere_Agent                      # Django app
│   ├── SBC_Tools.py                        # Four MCP tools
│   ├── SIPp_MCP_Svr_Cohere_v1.py           # MCP server + Cohere LLM analysis
│   ├── basic_call.xml                      # SIPp XML Scenario file
│   ├── config.json                         # Credentials
│   ├── test_logs                           # Folder where SIP logs are kept
│   ├── urls.py
│   └── views.py                            # Async webhook handler + pipeline
├── db.sqlite3
└── manage.py

```

---

## Configuration

Create `Webex_Cohere_Agent/config.json`:

```json
{
  "CUBE_USER" : "your-cube-ssh-username",
  "CUBE_PASS" : "your-cube-ssh-password",
  "SIPP_LOCAL_IP" : "ip-of-sipp-host",
  "LLM_PROVIDER" : "cohere",
  "LLM_API_KEY" : "your-cohere-api-key", 
  "WEBEX_BOT_TOKEN" : "your-webex-bot-token",
  "BOT_EMAIL" : "your-bot-username@webex.bot",
}
```

## Running

### 1. Configure Webex bot

In the [Webex Developer Portal](https://developer.webex.com/my-apps):

- Click on **Create New App**
- Click on **Onboard Bot**.
- **Bot Name:** Give it a name.
- **Bot Username:** A unique username for the bot.
- **Description:** Provide a description.
- Click on **Add Bot**.
- Copy the newly generated **Token Value**
  
### 2. Start the MCP server

```bash
cd SIPp_Webex_Cohere/Webex_Cohere_Agent
uvicorn SIPp_MCP_Svr_Cohere_v1:app --host localhost --port 3333
```

The MCP server must be running on `localhost:3333` before Django starts.

### 3. Expose with ngrok (development)

```bash
ngrok http 8000
```

### 4. Start Django with uvicorn

```bash
cd SIPp_Webex_Cohere
uvicorn SIPp_Webex_Cohere.asgi:application --host 0.0.0.0 --port 8000
```
> ⚠️ Do **not** use `manage.py runserver`. The webhook view is async & has a custom asgi.py that registers your Webex webhook at startup which requires an ASGI server instead of the default WSGI server.


### 5. Webex app

- Login to Webex app and search for your Bot using the username you assigned in Step 1.
- Now you can talk to it 1:1 like any other member of your organization. 
- Alternatively, you can add your bot as a person in your Webex space and chat with it by explicitly calling it.

---

## MCP Tools

| Step | Tool | Description |
|------|------|-------------|
| 1 | `enable_debug` | SSH into the CUBE and enable `debug ccsip messages` |
| 2 | `run_sipp_test` | Run a SIPp UAC scenario to the target extension; capture Call-ID and outcome |
| 3 | `disable_and_pull_logs` | Disable debug, pull the full log buffer over SSH, save lines filtered by Call-ID |
| 4 | `read_filtered_log` | Send filtered log to Cohere; return structured summary, root cause, and recommended action |

---

## Notes

- Tested against Cisco CUBE running IOS-XE
- SIPp must be installed and on the system `PATH` on the machine running the Django server
- The MCP server must be started before the Django app
- For production deployment, replace ngrok with a proper reverse proxy (nginx, etc.) and a static domain
- The bot does not verify media and it does not test call quality. A “successful” result in the SIP sense only means the signalling completed without errors

---

## Related Blog Posts

- [Part 1 - Stop Testing Phone Numbers Manually: Let SIPp Do It](https://learnuccollab.com/2026/06/21/automated-testing-phone-numbers-with-sipp/)
- [Part 2 - SIPp at Scale: Driving Bulk Automated Call Tests with Python](https://learnuccollab.com/2026/08/09/sipp-at-scale-driving-bulk-automated-call-tests-with-python/)
- [Part 3 - Build a Django GUI for SIP Call Testing](https://learnuccollab.com/2026/09/20/build-a-django-gui-for-sip-phone-call-testing/)
- [Part 4 - Enhance SIP VoIP Call Testing with Agentic AI LLM Integration](https://learnuccollab.com/2026/09/26/enhance-sip-voip-call-testing-with-agentic-ai-llm-integration/)

---

## Related YouTube Explanatory Videos

- [Stop Testing Phone Numbers Manually: Let SIPp Do It](https://youtu.be/inulTQSeWwQ?si=iHankRLgPV70kgoQ)
- [SIPp at Scale: Driving Bulk Automated Call Tests with Python](https://youtu.be/zdwOsXrXJDA?si=beGKkOgXsKoMbR04)
- [CLI to GUI: Automating SIPp VoIP Call Testing with Django & Python](https://youtu.be/-tDElcYFS4A?si=QBQGjG0pyn6k6Vfn)
- [Build LLM AI Agents to enhance SIP VoIP Testing](https://www.youtube.com/watch?v=3YeSeaIesjk)
