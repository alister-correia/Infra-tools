# Infra Assistant

A browser-based infrastructure operations tool for VMware vCloud Director (VCD). Use the task panel to list, inspect, and manage VCD resources — VMs, vApps, networks, edge gateways, firewall rules, NAT rules, security groups, and more.

---

## Privacy & Data Storage

**No data is stored anywhere by this tool.**

- All VCD credentials (username, password) are stored exclusively in your **browser's sessionStorage** — they exist only for the lifetime of that browser tab and are automatically cleared when the tab is closed
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
- Multi-environment support — switch between VCD environments from the UI

---

## Requirements

- Python 3.9+
- Access to one or more VMware VCD environments

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
│       ├── tasks.py         # VCD task endpoints
│       └── vcd.py           # VCD env/org/VDC selector endpoints
├── connectors/
│   └── vcd_client.py        # VCD REST API client
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
