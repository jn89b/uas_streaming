#!/bin/bash
set -e

echo "=== Installing Docker Engine on Debian ==="

# Remove conflicting packages if present
sudo apt remove -y \
    docker.io \
    docker-compose \
    docker-doc \
    docker-buildx \
    podman-docker \
    containerd \
    runc 2>/dev/null || true

# Install prerequisites
sudo apt update
sudo apt install -y ca-certificates curl

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings

sudo curl -fsSL \
    https://download.docker.com/linux/debian/gpg \
    -o /etc/apt/keyrings/docker.asc

sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add Docker repository
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

# Install Docker
sudo apt update

sudo apt install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

# Enable Docker
sudo systemctl enable --now docker

# Allow current user to run Docker without sudo
sudo usermod -aG docker "$USER"

echo
echo "=========================================="
echo "Docker installation complete."
echo "=========================================="
echo
echo "Run:"
echo
echo "    newgrp docker"
echo
echo "Then test:"
echo
echo "    docker run hello-world"
echo
echo "    docker ps"
echo
echo "    docker compose version"
newgrp docker