# Infra Assistant

A natural-language infrastructure operations tool for VMware vCloud Director (VCD). Type plain English to list, inspect, and manage VCD resources — VMs, vApps, networks, edge gateways, firewall rules, NAT rules, security groups, and more.

Powered by Claude (Anthropic) for intent parsing and a FastAPI backend that talks directly to the VCD REST API.

---

## Features

- List and inspect VMs, vApps, networks, edge gateways, firewall/NAT rules, security groups, IP sets
- Get full VM details — CPU, RAM, disks, NICs
- Get full vApp details — child VMs and connected networks
- Edit VM compute (CPU / memory)
- Create org VDC networks, edge firewall rules, NAT rules, security groups, IP sets
- Dry-run mode — preview any write operation before executing
- Multi-environment support — switch between VCD environments from the UI
- Per-user login — credentials are never stored server-side

---

## Requirements

- Python 3.9+
- Access to one or more VMware VCD environments
- An Anthropic API key (entered at login in the UI)

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

> The Anthropic API key is **not** stored in `.env`. It is entered per-user at login through the UI.

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
3. Enter your Anthropic API key
4. Select your VCD environment and org from the dropdowns
5. Select a VDC — all operations are scoped to the selected VDC

---

## Usage

Type natural-language commands in the chat panel, or use the task cards on the right panel for common operations.

**Example commands:**

```
list all vms
show vapp details for my-app
list networks
show firewall rules for EdgeGW-01
list nat rules
get vm details for web-01
edit vm web-01 — set cpu to 4 and memory to 8gb
create a routed network called app-net with gateway 10.10.1.1/24
```

Write operations (create, edit) go through a **dry-run preview** first. You confirm before anything is executed.

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
│       ├── chat.py          # NL chat endpoint
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

- The VCD tenant account used for login must have at least read access to the target VDC
- `adminVApp` query type is used for vApp listing — this requires org-admin or equivalent tenant role
- CPU and memory values for VMs are fetched individually per VM; listing many VMs may take a few seconds
