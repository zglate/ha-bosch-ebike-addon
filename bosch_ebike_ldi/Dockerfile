# Fixed base: since Supervisor 2026.04 BUILD_FROM is no longer passed.
FROM ghcr.io/home-assistant/base:3.24

# Alpine packages only: dbus-python and PyGObject have no wheels, and paho 1.6
# matches the API the upstream bridge was written against.
RUN apk add --no-cache \
        python3 \
        py3-dbus \
        py3-gobject3 \
        py3-paho-mqtt

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
