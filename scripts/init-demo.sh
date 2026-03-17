#!/usr/bin/env sh
set -eu

docker compose up --build -d
echo "前端: http://localhost:5173"
echo "后端: http://localhost:5174"
