#!/bin/bash
set -e

cd /opt/apps/flask-app

echo "Pulling latest code..."
git fetch origin
git checkout vps/main
git pull origin vps/main

echo "Building and restarting Flask container..."
docker compose up -d --build

echo "Cleaning Docker build cache..."
docker builder prune -af

echo "Flask deployment completed."
