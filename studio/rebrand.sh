#!/bin/sh
# Rebrands the built Postiz frontend as Shipcast. Runs inside the image build (see Dockerfile).
# AGPL note: this directory (Dockerfile + this script + public/ + brand.css) is the full set of changes to upstream.
set -eu
cd /app/apps/frontend
FILES=$(mktemp)

# 1. Name. "Postiz" (capitalised) only ever appears in display strings in the build, never in code identifiers.
grep -rlI --exclude-dir=cache 'Postiz' .next > "$FILES"
xargs -r sed -i 's/Postiz/Shipcast/g' < "$FILES"
echo "name: $(wc -l < "$FILES") files"

# 2. Colours (purple -> orange; #CF4410 keeps white button text at WCAG AA, 4.7:1), the Shipcast MCP guide in
#    Settings and on the Agent page, menu item "Claude". All in brand.js; it fails the build if a patch misses.
node "$(dirname "$0")/brand.js"

# 2b. Built chunks keep upstream file names, so after a rebrand their content changes under the same URL. Serve
#     them "no-cache" (browsers and Cloudflare revalidate with ETag, cheap 304s) so changes show without purges.
sed -i 's#^        location / {#        location /_next/static/ {\n            proxy_pass http://localhost:4200;\n            proxy_hide_header Cache-Control;\n            add_header Cache-Control "no-cache" always;\n        }\n\n        location / {#' /etc/nginx/nginx.conf
grep -q 'location /_next/static/' /etc/nginx/nginx.conf

# 3. Brand CSS layer: each stylesheet chunk imports /shipcast.css (public/, served with max-age=0).
#    Hashed chunks are cached for a year by browsers and Cloudflare; the import keeps brand tweaks live without purges.
for f in .next/static/chunks/*.css; do { echo '@import url("/shipcast.css");'; cat "$f"; } > "$f.new" && mv "$f.new" "$f"; done

# self-check: nothing left to rebrand (if-blocks, because set -e ignores `! cmd`)
if grep -rqI --exclude-dir=cache 'Postiz' .next; then echo "FAIL: Postiz still present"; exit 1; fi
if grep -rqIiE --exclude-dir=cache '#612bd3|#fc69ff|rgb\(97 43 211' .next; then echo "FAIL: old colours present"; exit 1; fi
grep -q 'shipcast.css' .next/static/chunks/*.css
grep -q 'Shipcast brand layer' public/shipcast.css
rm -f "$FILES"
echo "rebrand ok"
