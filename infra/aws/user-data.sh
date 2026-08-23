#!/usr/bin/env bash
# EC2 bootstrap, passed as user-data. Runs once, as root, on first boot.
#
# Deliberately small. Everything this does is either impossible later (creating
# the user the deploy will SSH as) or needed before the first deploy can run
# (Docker). Application configuration arrives with the deploy, not here -- a
# bootstrap script that also configures the app is a second place the app is
# defined, and it only runs once, so it silently rots.
set -euxo pipefail

# Amazon Linux 2023.
dnf update -y
dnf install -y docker git

systemctl enable --now docker

# The deploy user. Not `ec2-user`: a deploy key that can also administer the box
# is a deploy key that is worth stealing. This account can drive Docker and
# nothing else.
useradd --create-home --shell /bin/bash deploy || true
usermod -aG docker deploy

# The compose plugin, installed for all users rather than via `pip install
# docker-compose` (the v1 Python implementation, which is end-of-life).
COMPOSE_VERSION=v2.40.1
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL \
  "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

install -d -o deploy -g deploy /opt/resumeforge

# Log rotation. A t3.micro has 8GB of disk and an unbounded json-file log will
# eventually fill it -- which presents as the database failing to write, not as
# a logging problem.
cat >/etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
systemctl restart docker

# Swap. A t3.micro has 1GB of RAM and the AI service alone asks for 512MB;
# without swap the kernel OOM-kills whichever container asked for memory most
# recently, which is rarely the one at fault.
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi

echo "bootstrap complete" >/var/log/resumeforge-bootstrap.done
