# Oracle Ampere A1 deployment

This deployment uses the ARM64 Node/Playwright bridge. Do not use the native
Zoom C++ SDK Dockerfile on an Ampere VM; that path is built for x86_64.

## 1. Create the VM

In Oracle Cloud Infrastructure:

1. Create an account and choose the nearest home region.
2. Create a Compute instance.
3. Choose Ubuntu 24.04 ARM64.
4. Choose `VM.Standard.A1.Flex`.
5. Allocate 2 OCPUs and 12 GB memory.
6. Add an SSH public key.
7. Add ingress rules for TCP 22, 80, and 443 only.

If Oracle reports out of host capacity, select another availability domain or
retry later.

## 2. Prepare Ubuntu

```bash
ssh ubuntu@YOUR_VM_IP
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin
sudo usermod -aG docker "$USER"
exit
ssh ubuntu@YOUR_VM_IP
```

## 3. Install Heimdall

```bash
git clone https://github.com/abhishek9sharma11-dev/Heimdall.git
cd Heimdall
cp .env.oracle.example .env.oracle
nano .env.oracle
```

Set the Slack values in `.env.oracle` if report delivery is required. Keep the
file private.

## 4. Start the service

```bash
docker compose -f docker-compose.oracle.yml up -d --build
docker compose -f docker-compose.oracle.yml ps
curl http://127.0.0.1:8780/health
```

The service is configured with Docker init, a 1 GB shared-memory area, and
`restart: unless-stopped`. Uploaded schedules and runtime session manifests
are kept in the Docker schedules volume and are deleted by the normal
end-of-session cleanup.

## 5. Start on VM reboot

```bash
sudo tee /etc/systemd/system/heimdall.service >/dev/null <<'UNIT'
[Unit]
Description=Heimdall Zoom bot
Requires=docker.service
After=docker.service

[Service]
WorkingDirectory=/home/ubuntu/Heimdall
ExecStart=/usr/bin/docker compose -f docker-compose.oracle.yml up
ExecStop=/usr/bin/docker compose -f docker-compose.oracle.yml down
Restart=always
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now heimdall
```

Open the dashboard at `http://YOUR_VM_IP:8780/connect.html` while testing. Put
HTTPS in front of it with Caddy before using it publicly.
