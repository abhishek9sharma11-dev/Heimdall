# Hosting Heimdall AI Webinar Bot

This repo is containerized and designed to run on a Linux host. The Zoom Linux SDK requires Linux and an X display, so the safest hosting target is Ubuntu 22.04 / 24.04.

## Recommended hosting setup

1. Provision a Linux VM or server.
   - Ubuntu 22.04 or 24.04
   - 2GB+ RAM, 4GB+ recommended
   - Public internet access for Zoom and LLM API calls

2. Install Docker and Docker Compose.

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg lsb-release
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in after adding yourself to the `docker` group.

## Prepare the repo

```bash
git clone https://github.com/<your-repo-url>.git
cd heimdall
cp .env.example .env
chmod +x run.sh stop.sh
```

## Configure the bot

Edit `.env` and set at least these values:

- `ZOOM_BACKEND=bridge`
- `MEETING_ID=` your webinar / meeting ID
- `MEETING_PASSWORD=` passcode if required
- `ZOOM_SDK_KEY=` your Zoom SDK key
- `ZOOM_SDK_SECRET=` your Zoom SDK secret
- `OPENROUTER_API_KEY=` or local Ollama credentials
- `OPENROUTER_BASE_URL=` if using a local LLM endpoint
- `ANTHROPIC_MODEL=` model name or local model alias
- `HOST_EMAIL=` host email for slash command control
- `BOT_DISPLAY_NAME=` must contain `AI` or `Assistant`

If you have a schedule file, copy it into the repository root as `schedule.json`:

```bash
cp schedules/ai-for-students-day2.json schedule.json
```

## Install the Zoom SDK

Download the official Zoom Linux SDK and place the extracted SDK directory in:

```bash
bridge/zoomsdk/
```

The C++ bridge build expects the SDK files under `bridge/zoomsdk/`.

## Start the service

From the repo root:

```bash
./run.sh
```

This script will:
- try Docker Compose first
- build the image
- start the container
- if Docker is unavailable, fall back to a native Python run

## Stop the service

```bash
./stop.sh
```

This script will:
- stop Docker Compose if the container mode is running
- otherwise kill the native `zoom-bridge` and `python -m src.main` processes

## Monitor the service

If using Docker:

```bash
docker compose logs -f
```

If you want to see current containers:

```bash
docker compose ps
```

## Update the bot

```bash
git pull
./stop.sh
./run.sh
```

## Optional: run systemd-managed Docker service

If you want the bot to start automatically after reboot, create a systemd unit like this:

```ini
[Unit]
Description=Heimdall AI Webinar Bot
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/home/ubuntu/heimdall
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Save it as `/etc/systemd/system/heimdall.service` and then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now heimdall.service
```

## Notes

- The container does not expose any public ports by default.
- Keep the `.env` file private; it contains your Zoom credentials and LLM keys.
- Use `stop.sh` before changing `.env` or restarting the meeting.
