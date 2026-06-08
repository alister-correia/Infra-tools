# Infra Assistant

A natural-language infrastructure operations tool for VMware vCloud Director (VCD). Use the task panel to list, inspect, and manage VCD resources — VMs, vApps, networks, edge gateways, firewall rules, NAT rules, security groups, and more.

An optional AI chat mode (powered by Claude) lets you type plain English commands instead of using the task cards.

---

## Privacy & Data Storage

**No data is stored anywhere by this tool.**

- All VCD credentials (username, password) and your Anthropic API key are stored exclusively in your **browser's sessionStorage** — they exist only for the lifetime of that browser tab and are automatically cleared when the tab is closed
- Credentials are never written to disk, never logged, and never persisted on the server
- Every API call to VCD is a **live fetch** — there is no caching, no database, and no session state on the server side
- Each request from the browser sends the credentials as HTTP headers directly to the backend, which uses them to authenticate with VCD in real time and returns the result immediately
- The server holds no state between requests

---

## How Login Works

1. You enter your VCD username and password in the login form
2. The browser stores them in `sessionStorage` (tab-only, never written to disk)
3. On login, the tool makes a live request to VCD to fetch the list of organisations your account has access to — this confirms the credentials are valid
4. You select your org from the dropdown — another live VCD call fetches the VDCs available in that org
5. You select a VDC — all subsequent operations are scoped to it
6. Every task or query you run sends your credentials as request headers to the backend, which authenticates with VCD on the fly and returns the live result
7. When you close the tab or click **Sign Out**, all credentials are wiped from `sessionStorage` immediately

**The server never stores, logs, or caches your credentials at any point.**

---

## Features

- List and inspect VMs, vApps, networks, edge gateways, firewall/NAT rules, security groups, IP sets
- Get full VM details — CPU, RAM, disks, NICs
- Get full vApp details — child VMs and connected networks
- Edit VM compute (CPU / memory)
- Create org VDC networks, edge firewall rules, NAT rules, security groups, IP sets
- Dry-run mode — preview any write operation before executing
- Multi-environment support — switch between VCD environments from the UI
- **Optional AI mode** — toggle on to use natural-language chat; requires an Anthropic API key

---

## Requirements

- Python 3.9+
- Access to one or more VMware VCD environments
- An Anthropic API key — **only needed if you want to use AI chat mode**

---

## Setup

### 1. Clone the repo

```bash
git clone git@github.com:alister-correia/Infra-tools.git
cd Infra-tools
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Create the `.env` file

Copy the example and edit it:

```bash
cp .env.example .env
```

Open `.env` and set your VCD environments:

```env
VCD_ENVIRONMENTS=Name1:https://vcloud-host1.yourdomain.com,Name2:https://vcloud-host2.yourdomain.com
```

**Format:** `DisplayName:https://hostname` — comma-separated, one entry per VCD environment.

Example with two environments:

```env
VCD_ENVIRONMENTS=Production:https://vcloud.corp.com,DR:https://vcloud-dr.corp.com
```

> The `.env` file contains your VCD host URLs and is excluded from version control. Never commit it.

### 4. Start the server

```bash
python3 main.py
```

The app runs on **http://localhost:3030** by default.

To run in the background:

```bash
nohup python3 main.py > logs/server.log 2>&1 &
```

---

## Logging in

1. Open **http://localhost:3030** in your browser
2. Enter your VCD username and password
3. The tool fetches your accessible orgs live from VCD to confirm the credentials
4. Select your org — VDCs are fetched live from VCD
5. Select a VDC — all operations are scoped to it

No API key is needed at this stage. The tool is fully usable without one.

---

## AI Mode (optional)

The UI has an **AI toggle** in the top bar. When switched on:

- A chat panel appears where you can type plain-English commands
- You will be prompted to enter your **Anthropic API key**
- The key is stored only in your browser tab (`sessionStorage`) and is cleared when the tab is closed
- It is never sent to the server except as a request header to make the Claude API call — it is not logged or stored

**Example commands in AI mode:**
```
list all vms
show vapp details for my-app
list firewall rules for EdgeGW-01
edit vm web-01 — set cpu to 4 and memory to 8gb
create a routed network called app-net with gateway 10.10.1.1/24
```

Write operations go through a **dry-run preview** first — you confirm before anything is executed.

To get an Anthropic API key: [console.anthropic.com](https://console.anthropic.com)

---

## Project Structure

```
infra-assistant/
├── main.py                  # FastAPI entrypoint
├── requirements.txt
├── .env                     # Your config (not committed)
├── .env.example             # Template
├── api/
│   └── routes/
│       ├── chat.py          # AI chat endpoint
│       ├── tasks.py         # Direct VCD task endpoints
│       └── vcd.py           # VCD env/org/VDC selector endpoints
├── connectors/
│   └── vcd_client.py        # VCD REST API client
├── services/
│   ├── intent_parser.py     # Claude-powered NL → structured intent
│   ├── executor.py          # Executes confirmed write operations
│   ├── query_executor.py    # Executes read/query operations
│   └── dry_run.py           # Dry-run plan builder
├── models/
│   └── schemas.py           # Pydantic models
├── config/
│   └── settings.py          # App settings (loaded from .env)
└── frontend/
    └── index.html           # Single-page UI
```

---

## Notes

- The VCD account used must have at least read access to the target VDC
- `adminVApp` query type is used for vApp listing — requires org-admin or equivalent tenant role
- CPU and memory are fetched individually per VM; listing many VMs may take a few seconds
