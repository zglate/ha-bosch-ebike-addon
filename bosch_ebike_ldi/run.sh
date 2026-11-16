#!/command/with-contenv bashio
# Start the Bosch eBike Live Data bridge.
# MQTT details come from the Mosquitto add-on; options.py reads the rest
# from /data/options.json.

if bashio::services.available "mqtt"; then
    export MQTT_HOST="$(bashio::services mqtt "host")"
    export MQTT_PORT="$(bashio::services mqtt "port")"
    export MQTT_USERNAME="$(bashio::services mqtt "username")"
    export MQTT_PASSWORD="$(bashio::services mqtt "password")"
else
    bashio::log.warning "No MQTT service found; using mqtt_host from the options if set"
fi

export ADDON_VERSION="$(bashio::addon.version)"

# host_dbus mounts the host's system bus at /run/dbus.
export DBUS_SYSTEM_BUS_ADDRESS="unix:path=/run/dbus/system_bus_socket"

bashio::log.info "Starting Bosch eBike Live Data bridge"
exec python3 -u /app/bridge.py /data/options.json
