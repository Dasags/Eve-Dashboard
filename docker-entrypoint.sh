#!/bin/sh
D="${DATA_DIR:-/data}"
echo "[entrypoint] uid=$(id -u) data=$D owner=$(stat -c '%u:%g %a' "$D" 2>/dev/null || echo fehlt)"
mkdir -p "$D" 2>/dev/null
chown eve "$D" 2>/dev/null || echo "[entrypoint] WARNUNG: chown auf $D fehlgeschlagen (fehlt CAP_CHOWN, NFS/Bind-Mount oder read_only?)"
if [ "$(id -u)" = "0" ]; then
  set -- setpriv --reuid=eve --regid=eve --clear-groups "$@"
fi
if ! setpriv --reuid=eve --regid=eve --clear-groups sh -c "touch '$D/.w' && rm '$D/.w'" 2>/dev/null; then
  echo "[entrypoint] FEHLER: $D ist für User eve (uid 1000) nicht beschreibbar. Auf dem Host: chown -R 1000:1000 <Pfad> oder Volume neu anlegen."
fi
exec "$@"
