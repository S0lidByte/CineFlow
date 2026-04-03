#!/bin/sh

# Default PUID and PGID to 1000 if not set
PUID=${PUID:-1000}
PGID=${PGID:-1000}

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
MOUNT_COUNT=$(grep -c "rivenvfs" /proc/mounts || true)
if [ "$MOUNT_COUNT" -gt 1 ]; then
    echo "Duplicate FUSE mount detected ($MOUNT_COUNT entries). Cleaning up..."
    # A lazy unmount clears the top/duplicate layer inside this namespace 
    # without breaking the lower layer that propagated to the host.
    umount -l /mnt/rivenfs || true
fi

# Bring the main program back to the foreground so logs pass through and SIGTERMs work
wait $MAIN_PID
