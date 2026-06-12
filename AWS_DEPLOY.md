# Deploying on AWS EC2 (Docker Compose)

Full walkthrough from zero. End state: the web dashboard running on an EC2
instance, reachable only from IP addresses you trust.

---

## 0. What you'll end up with

```
Your laptop ──SSH(22)──▶  EC2 (Ubuntu 24.04, Docker)
Your trusted IP ─(5000)─▶  ┌───────────────────────────┐
                           │ docker compose             │
                           │  └─ webui  → app.py :5000   │
                           │     (Playwright + Chromium) │
                           └───────────────────────────┘
```

---

## 1. Launch the EC2 instance (AWS Console)

1. **EC2 → Launch instance**
2. **Name:** `lead-automation`
3. **AMI:** *Ubuntu Server 24.04 LTS* (x86_64)
4. **Instance type:** `t3.small` (2 GB RAM) minimum — Chromium will OOM on a
   1 GB free-tier `t2.micro`. `t3.medium` (4 GB) if you'll run several offers.
5. **Key pair:** create one (e.g. `lead-automation-key`), download the `.pem`,
   keep it safe — it's your SSH login.
6. **Network settings → Edit → Security group** (create new), add inbound rules:

   | Type        | Port | Source                       | Why                |
   |-------------|------|------------------------------|--------------------|
   | SSH         | 22   | *My IP*                      | You, to log in     |
   | Custom TCP  | 5000 | *Custom* → your trusted IP/32 | Dashboard access  |

   > Use `<your.ip.address>/32` for a single IP. Add more rules for more
   > trusted IPs. **Do not** use `0.0.0.0/0` on port 5000 — that opens the
   > dashboard to the whole internet.
7. **Storage:** 20 GB gp3 (Chromium + logs + screenshots need room).
8. **Launch.** Note the instance's **Public IPv4 address** (call it `EC2_IP`).

---

## 2. SSH into the instance

```bash
chmod 400 ~/Downloads/lead-automation-key.pem
ssh -i ~/Downloads/lead-automation-key.pem ubuntu@EC2_IP
```

---

## 3. Install Docker + Compose plugin (on the instance)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

# Run docker without sudo (log out/in afterward, or run `newgrp docker`)
sudo usermod -aG docker $USER
newgrp docker
docker --version && docker compose version
```

---

## 4. Get the code onto the instance

**Option A — git (if the repo is on GitHub):**
```bash
git clone https://github.com/<you>/50kLoonsAutomation-Tool.git
cd 50kLoonsAutomation-Tool
```

**Option B — copy from your laptop (no GitHub).** Run this *on your laptop*,
excluding venv/secrets/artifacts:
```bash
rsync -av -e "ssh -i ~/Downloads/lead-automation-key.pem" \
  --exclude venv --exclude .git --exclude __pycache__ \
  --exclude logs --exclude screenshots --exclude '*.bak' \
  ~/Desktop/50kLoonsAutomation-Tool/ \
  ubuntu@EC2_IP:~/50kLoonsAutomation-Tool/
```
Then on the instance: `cd ~/50kLoonsAutomation-Tool`

---

## 5. Add secrets (these are git-ignored — they must be placed manually)

**`.env`** — copy the template and fill in real values:
```bash
cp .env.example .env
nano .env
```
Make sure `HEADLESS=true` (required inside a container) and fill in your
`SHEET_URL_*`, `ROTATING_PROXY_*`, etc.

**Google service-account key** — from your laptop:
```bash
scp -i ~/Downloads/lead-automation-key.pem \
  ~/Desktop/50kLoonsAutomation-Tool/credentials/credentials.json \
  ubuntu@EC2_IP:~/50kLoonsAutomation-Tool/credentials/credentials.json
```

**Proxies** (if you use `PROXY_SOURCE=file`):
```bash
scp -i ~/Downloads/lead-automation-key.pem \
  ~/Desktop/50kLoonsAutomation-Tool/proxies.txt \
  ubuntu@EC2_IP:~/50kLoonsAutomation-Tool/proxies.txt
```

---

## 6. Choose your run model (important)

The shipped `docker-compose.yml` runs **two** services that would both pull the
same pending rows. Pick one:

- **Dashboard-controlled (recommended for you):** run only `webui`.
  ```bash
  docker compose up -d --build webui
  ```
- **Hands-off auto-loop (no UI):** run only `automation`.
  ```bash
  docker compose up -d --build automation
  ```

> First build downloads Chromium + deps — expect a few minutes.

---

## 7. Use it

- Dashboard: open **http://EC2_IP:5000** from your trusted IP.
- Logs:        `docker compose logs -f webui`
- Restart:     `docker compose restart webui`
- Stop:        `docker compose stop`         (graceful, waits for current row)
- Tear down:   `docker compose down`

---

## 8. Updating after code changes

```bash
# pull or rsync the new code, then:
docker compose up -d --build webui
```

---

## 9. Cost / housekeeping

- A `t3.small` left running 24/7 is ~$15/mo (on-demand, us-east-1). **Stop** the
  instance from the console when idle to avoid charges (you keep the disk).
- Screenshots accumulate in `screenshots/` — they're volume-mounted to the host,
  so clear them periodically: `rm -f screenshots/*/*.png`.
- Logs are capped by the `logging` config in `docker-compose.yml` (json-file,
  rotated) — no action needed.

## Security notes

- Port 5000 has **no app-level password** — your protection is the security
  group IP allow-list. Keep it to trusted `/32` addresses only.
- `.env` and `credentials/credentials.json` hold secrets. They live only on the
  instance, are git-ignored, and are never baked into the image.
