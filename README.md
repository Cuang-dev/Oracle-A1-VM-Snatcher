# Oracle A1 VM Snatcher 🚀

An ultra-lightweight, Docker-ready, and automated tool designed to provision Oracle Cloud Infrastructure (OCI) **"Always Free" Ampere A1 ARM Compute instances** (up to 4 OCPUs, 24 GB RAM) and AMD instances, bypassing the notorious *"Out of host capacity"* constraint.

Includes a modern Dark-Theme Web Management Portal, Telegram notification alerts, and auto-chat discovery.

---

## ⚡ Highlights & Resource Efficiency

- **Ultra-Low Memory Footprint**: Uses **~45–50 MB RAM** at runtime. Runs single-process multi-threaded Gunicorn (`gthread`) to avoid duplicating heavy SDK heaps across worker processes.
- **Zero Idle CPU**: Uses event-based synchronization (`threading.Event.wait()`) during retry intervals—consuming **0.0% CPU** between attempts.
- **Bounded In-Memory Logs**: Employs `collections.deque(maxlen=1000)` to ensure memory never leaks or expands indefinitely over weeks of retries.
- **Fast Multi-Stage Docker Build**: Built on `python:3.12-slim` with multi-stage compilation, pip caches purged, and non-root execution (`appuser:10001`).
- **Telegram Bot Integration**:
  - Pre-configured with bot token `8662502307:AAF_5VwOGGK5tyEoEU6ujqcRLZulcgL9dXU` ([@cuanglabbot](https://t.me/cuanglabbot)).
  - **Auto-Detect Chat ID**: One-click automatic detection of your Telegram chat via `getUpdates` API.
  - Sends instant notifications on instance creation (with Public IP, shape, AD, and region), loop start/stop, and critical errors.
- **Full OCI Provisioner Portal Features**:
  - Visual Free Tier quota check (ARM OCPUs, RAM, Micro instances, Block Volume GB).
  - Raw OCI config parser (paste or upload `.txt`).
  - Private key uploader (`.pem` / `.key`).
  - OS Image scanning (Ubuntu filter or All-OS mode).
  - Automated or custom subnet & VNIC selection.
  - Custom Ampere A1 Flex shape configuration (custom OCPUs and Memory).
  - Security list / Firewall rule manager (open ports 22, 80, 443, etc.).
  - Randomized or fixed delay retry intervals (25–60s) to avoid rate limits.

---

## 🐳 Quick Start with Docker

### Option 1: Docker Compose (Recommended)

1. Clone or navigate to the repository:
   ```bash
   git clone https://github.com/Cuang-dev/Oracle-A1-VM-Snatcher.git
   cd Oracle-A1-VM-Snatcher
   ```

2. Start the container in the background:
   ```bash
   docker compose up -d --build
   ```

3. Open your browser:
   ```
   http://localhost:5000
   ```

To stop:
```bash
docker compose down
```

### Option 2: Docker CLI

Build the ultra-light image:
```bash
docker build -t oracle-a1-vm-snatcher .
```

Run with resource limits:
```bash
docker run -d \
  --name oracle-a1-vm-snatcher \
  --restart unless-stopped \
  --memory=256m \
  --cpus=0.5 \
  -p 5000:5000 \
  -e PORT=5000 \
  -e TELEGRAM_BOT_TOKEN="8662502307:AAF_5VwOGGK5tyEoEU6ujqcRLZulcgL9dXU" \
  oracle-a1-vm-snatcher
```

---

## 📱 Telegram Notifications & Auto-Detect

The snatcher sends notifications directly to your Telegram chat.

### Connecting Your Telegram Chat:
1. Open Telegram and start a chat with [@cuanglabbot](https://t.me/cuanglabbot).
2. Tap **Start** or send `/start` (or any message).
3. In the Web UI under **3. Telegram alerts**, click **⚡ Auto-detect chat** (or click **Test connection**).
4. The system will auto-discover your Chat ID, link it, and send a confirmation message:
   > 🚀 **Oracle A1 VM Snatcher Connected!**  
   > Notifications for instance provisioning will be sent here.

### Interactive Bot Controls & Commands:
The Telegram bot ([@cuanglabbot](https://t.me/cuanglabbot)) includes an interactive keyboard and slash commands to control the snatcher remotely:

| Button / Command | Description |
|---|---|
| `⏸️ Pause` / `/pause` | Pauses the snatcher loop immediately without losing saved configurations |
| `▶️ Resume` / `/resume` | Resumes the snatcher loop using active/saved OCI credentials |
| `📊 Status` / `/status` | View real-time snatcher state, current attempt specs, attempts count, and uptime |
| `📋 Recent Logs` / `/logs` | View recent activity logs (`/logs 20` for up to 30 lines) |
| `📅 Daily Report` / `/daily` | Force-send a comprehensive 24h progress summary |
| `📡 Live Logs ON` / `/livelog on` | Stream every attempt result in real-time to Telegram |
| `🔕 Live Logs OFF` / `/livelog off` | Pause live attempt streaming |
| `🏓 Ping` / `/ping` | Test bot responsiveness |

> 💡 **Dynamic Pause/Resume Button**: The bot's reply keyboard dynamically switches between **⏸️ Pause** (when running) and **▶️ Resume** (when paused or idle) so you can toggle provisioning with a single tap.

When an instance is caught, you'll receive:
```
✅ OCI Provisioner Success!

Instance: AlwaysFree-ARM
Shape: VM.Standard.A1.Flex (2 OCPU, 12 GB RAM)
Region: us-ashburn-1
Public IP: 129.153.xx.xx
Time: 2026-09-14 19:30:00
Status: Running

Your Always Free instance has been successfully provisioned!
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5000` | HTTP port for the web interface |
| `APP_PASSWORD` | *(empty)* | Optional HTTP Basic Auth password for UI security |
| `MAX_ATTEMPTS` | `1000` | Maximum provisioning loop attempts before stopping |
| `TELEGRAM_BOT_TOKEN` | `8662502307:AAF_5VwOGGK5tyEoEU6ujqcRLZulcgL9dXU` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | *(auto-detected)* | Explicit Telegram Chat ID (or leave empty to auto-detect) |

---

## 🔑 OCI Credentials Setup

1. Log in to [Oracle Cloud Console](https://cloud.oracle.com/).
2. Click your profile icon in the top right $\rightarrow$ **User Settings**.
3. Under **Resources** (bottom left), select **API Keys** $\rightarrow$ **Add API Key**.
4. Choose **Generate API Key Pair**, download the **Private Key** (`.pem`), and click **Add**.
5. Copy the configuration block displayed on screen (contains `user`, `tenancy`, `fingerprint`, and `region`).
6. Paste the configuration text and upload your private key into the Web UI.

---

## 🛡️ Security Best Practices

- **Never commit your private keys or OCI config** into git or public repositories.
- If exposing the Web UI to the public internet, set `APP_PASSWORD` in `.env` to protect access with HTTP Basic Authentication.
- Once your instance has been captured and verified, stop the container or delete the credentials.
