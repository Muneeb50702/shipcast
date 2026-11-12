# Shipcast Studio = upstream Postiz (AGPL-3.0) + brand layer. Bump the tag here to update Postiz.
FROM ghcr.io/gitroomhq/postiz-app:v2.25.3
COPY public/ /app/apps/frontend/public/
COPY brand.css /app/apps/frontend/public/shipcast.css
# Shipcast Control, served on the Studio site: MCP link = <control>/mcp. Per install: --build-arg SHIPCAST_CONTROL=...
ARG SHIPCAST_CONTROL=https://social.muhammadmuneeb.tech/control
COPY rebrand.sh brand.js /tmp/
RUN SHIPCAST_CONTROL="$SHIPCAST_CONTROL" sh /tmp/rebrand.sh && rm /tmp/rebrand.sh /tmp/brand.js
