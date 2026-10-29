#!/usr/bin/env bash
# Deploys to a Google Cloud *Always Free* e2-micro VM (us-central1, 30 GB standard disk) running docker compose.
# Needs: gcloud CLI logged in (gcloud auth login), billing enabled on the project. Re-running redeploys the latest main.
set -euo pipefail
# Windows: the winget install of the SDK is not on PATH and needs its own Python
SDK="$(cygpath "${LOCALAPPDATA:-}" 2>/dev/null)/Google/Cloud SDK/google-cloud-sdk"
[ -d "$SDK" ] && { export PATH="$SDK/bin:$PATH" CLOUDSDK_PYTHON="$SDK/platform/bundledpython/python.exe"; }
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
PROJECT=${PROJECT:-n8n-gmail-481305}
ZONE=${ZONE:-us-central1-a}       # free-tier regions: us-west1, us-central1, us-east1
DISK_GB=${DISK_GB:-30}            # Always Free: 30 GB standard disk
VM=${VM:-salescore}
MACHINE=${MACHINE:-e2-micro}     # Always Free size (1 GB RAM, shared vCPU)
REPO=${REPO:-https://github.com/Insolutionsai-services/SalesEcosystem.git}
BRANCH=${BRANCH:-main}

gcloud config set project "$PROJECT" >/dev/null
gcloud services enable compute.googleapis.com >/dev/null

if ! gcloud compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$VM" --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family debian-12 --image-project debian-cloud \
    --boot-disk-size "${DISK_GB}GB" --boot-disk-type pd-standard --tags http-server,https-server \
    --metadata startup-script='#!/bin/bash
      set -e
      if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo "/swapfile none swap sw 0 0" >> /etc/fstab; fi
'
  gcloud compute firewall-rules describe allow-http >/dev/null 2>&1 || \
    gcloud compute firewall-rules create allow-http --allow tcp:80,tcp:443 --target-tags http-server
  echo "waiting for the VM to boot and install docker..."; sleep 90
fi

IP=$(gcloud compute instances describe "$VM" --zone "$ZONE" --format 'value(networkInterfaces[0].accessConfigs[0].natIP)')
ENV_FILE=${ENV_FILE:-.env}
[ -f "$ENV_FILE" ] || { echo "create $ENV_FILE first (copy .env.example; set ADMIN_KEY, OPENROUTER_API_KEY, SMTP_*, PUBLIC_BASE_URL=http://$IP)"; exit 1; }
DOMAIN=$(grep "^DOMAIN=" "$ENV_FILE" | cut -d= -f2)
gcloud compute scp "$ENV_FILE" "$VM:/tmp/.env" --zone "$ZONE" ${IAP:+--tunnel-through-iap}  # IAP=1: through Google's tunnel when port 22 is blocked
gcloud compute ssh "$VM" --zone "$ZONE" ${IAP:+--tunnel-through-iap} --quiet --command "
  set -e
  command -v docker >/dev/null || { curl -fsSL https://get.docker.com | sudo sh; sudo apt-get install -y git; }
  sudo mkdir -p /srv && cd /srv
  [ -d app ] || sudo git clone -b $BRANCH $REPO app
  cd app && sudo git fetch && sudo git checkout $BRANCH && sudo git pull
  sudo mv /tmp/.env .env
  sudo docker compose up -d --build
  sudo docker image prune -f >/dev/null"
echo "deployed: VM IP $IP  ->  point the DNS A record for $DOMAIN at it (Cloudflare proxy: DNS only or SSL mode Full strict)"
