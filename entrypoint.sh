#!/bin/sh

MOUNT_PATH="/mnt/rivenfs"

# Default PUID and PGID to 1000 if not set
PUID=${PUID:-1000}
PGID=${PGID:-1000}

cleanup_riven_mounts() {
    MAX_ATTEMPTS=${1:-20}
    ATTEMPT=0

    while grep -q " $MOUNT_PATH " /proc/mounts 2>/dev/null; do
        ATTEMPT=$((ATTEMPT + 1))
        echo "Cleaning stale RivenVFS mount at $MOUNT_PATH (attempt $ATTEMPT/$MAX_ATTEMPTS)..."

        fusermount3 -u -z "$MOUNT_PATH" 2>/dev/null || \
        fusermount -u -z "$MOUNT_PATH" 2>/dev/null || \
        umount -l "$MOUNT_PATH" 2>/dev/null || true

        sleep 1

        if [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
            echo "Failed to fully clean stale RivenVFS mounts after $MAX_ATTEMPTS attempts."
            break
        fi
    done
}

forward_signal() {
    SIGNAL="$1"

    echo "Received $SIGNAL, shutting down Riven..."

    if [ -n "${MAIN_PID:-}" ] && kill -0 "$MAIN_PID" 2>/dev/null; then
        kill -TERM "$MAIN_PID" 2>/dev/null || true
        wait "$MAIN_PID" 2>/dev/null || true
    fi

    cleanup_riven_mounts 20
    exit 0
}

trap 'forward_signal SIGINT' INT
trap 'forward_signal SIGTERM' TERM

echo "Starting Container with $PUID:$PGID permissions..."

if [ "$PUID" = "0" ]; then
    echo "Running as root user"
    USER_HOME="/root"
else
    # --- User and Group Management ---
    USERNAME=${USERNAME:-riven}
    GROUPNAME=${GROUPNAME:-riven}
    USER_HOME="/home/$USERNAME"
    if ! getent group "$PGID" > /dev/null; then addgroup --gid "$PGID" "$GROUPNAME"; fi
    GROUPNAME=$(getent group "$PGID" | cut -d: -f1)
    if ! getent passwd "$USERNAME" > /dev/null; then adduser -D -h "$USER_HOME" -u "$PUID" -G "$GROUPNAME" "$USERNAME"; fi
    usermod -u "$PUID" -g "$PGID" "$USERNAME"
    adduser "$USERNAME" wheel
fi

# Set home directory permissions and environment
mkdir -p "$USER_HOME"
chown -R "$PUID:$PGID" "$USER_HOME"
export HOME="$USER_HOME"

# Define the command to run based on the DEBUG flag
if [ "${DEBUG}" != "" ]; then
    echo "Installing debugpy..."
    /riven/.venv/bin/python -m ensurepip
    /riven/.venv/bin/python -m pip install debugpy
    CMD="/riven/.venv/bin/python -m debugpy --listen 0.0.0.0:5678 src/main.py"
else
    CMD="/riven/.venv/bin/python src/main.py"
fi


echo "Container Initialization complete."
echo "Starting Riven (Backend)..."

cleanup_riven_mounts 20

# Execute the command in the background
if [ "$PUID" = "0" ]; then
    RUN_CMD="$CMD"
else
    RUN_CMD="gosu $USERNAME $CMD"
fi

$RUN_CMD &
MAIN_PID=$!

echo "Waiting for RivenVFS FUSE mount to initialize..."
# Wait for the first 'rivenvfs' mount to register
while ! grep -q "rivenvfs" /proc/mounts; do
    sleep 1
done

# Give it an extra second to allow host propagation back to the container
sleep 1

# Check if the mount duplicated in the container's namespace
MOUNT_COUNT=$(grep -c " $MOUNT_PATH " /proc/mounts || true)
if [ "$MOUNT_COUNT" -gt 1 ]; then
    echo "Duplicate FUSE mount detected ($MOUNT_COUNT entries). Cleaning up..."
    cleanup_riven_mounts 20
fi

# Bring the main program back to the foreground so logs pass through and SIGTERMs work
wait $MAIN_PID
EXIT_CODE=$?
cleanup_riven_mounts 20
exit $EXIT_CODE
