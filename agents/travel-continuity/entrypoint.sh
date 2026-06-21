#!/bin/bash
# Copy mounted SSH key to writable location with correct perms (SSH refuses world-readable keys).
# Also seed known_hosts for github.com so the first push doesn't prompt.

set -e

if [ -f /opt/ssh-mount/id_ed25519 ]; then
  mkdir -p /root/.ssh
  cp /opt/ssh-mount/id_ed25519 /root/.ssh/id_ed25519
  if [ -f /opt/ssh-mount/id_ed25519.pub ]; then
    cp /opt/ssh-mount/id_ed25519.pub /root/.ssh/id_ed25519.pub
  fi
  chmod 700 /root/.ssh
  chmod 600 /root/.ssh/id_ed25519
  chmod 644 /root/.ssh/id_ed25519.pub 2>/dev/null || true

  if [ ! -s /root/.ssh/known_hosts ]; then
    ssh-keyscan -H github.com >> /root/.ssh/known_hosts 2>/dev/null || true
  fi

  # Git author identity for commits made by the container.
  git config --global user.name "${GIT_AUTHOR_NAME:-oru-travel-continuity}"
  git config --global user.email "${GIT_AUTHOR_EMAIL:-oru-travel@noreply.local}"
fi

exec "$@"
