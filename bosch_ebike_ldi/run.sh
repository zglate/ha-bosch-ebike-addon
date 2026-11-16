#!/command/with-contenv bashio
# Start the Bosch eBike bridge. It reads its options from /data/options.json
# and asks the Supervisor for the MQTT broker itself (waiting if needed).

# Newer bashio renamed addon.* to app.*; support both.
if declare -F bashio::app.version > /dev/null; then
    export ADDON_VERSION="$(bashio::app.version)"
else
    export ADDON_VERSION="$(bashio::addon.version)"
fi

# host_dbus mounts the host's system bus at /run/dbus.
export DBUS_SYSTEM_BUS_ADDRESS="unix:path=/run/dbus/system_bus_socket"

bashio::log.info "Starting Bosch eBike ${ADDON_VERSION}"
exec python3 -u /app/bridge.py /data/options.json
