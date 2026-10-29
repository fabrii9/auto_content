#!/bin/bash
# Despliega Contenido automatizado (auto_content) en el Odoo de Aftermoves.
# Solo toca /home/clientes/odooafm y el contenedor odooafm-odoo.
#
#   ./desplegar_odooafm.sh modulo   # sube/actualiza el módulo (sin reiniciar)
#   ./desplegar_odooafm.sh docker   # Chrome + ffmpeg en la imagen y recrea odooafm-odoo (1-2 min sin Odoo)
#   ./desplegar_odooafm.sh verificar
#
# Pide la passphrase de la llave. KEY=/otra/llave para usar otra; SSH_OPTS="..." suma opciones a ssh.
set -euo pipefail

KEY=${KEY:-/Users/fabrizio/llaves/keys_DO_Simba}
HOST=root@164.92.101.123
DIR=/home/clientes/odooafm
CONTENEDOR=odooafm-odoo
DOCKERFILE_ORIGINAL_MD5=665ae6b069a3d1c0e610cc742b0aaee5
AQUI=$(cd "$(dirname "$0")" && pwd)
MODULO=$(dirname "$AQUI")

remoto() { ssh -o IdentitiesOnly=yes -o LogLevel=ERROR ${SSH_OPTS:-} -i "$KEY" "$HOST" "$@"; }

modulo() {
  echo "→ Subiendo auto_content a $DIR/addons_custom"
  # Lo temporal y la copia anterior van fuera de addons_custom: Odoo rechaza
  # cualquier carpeta con nombre de módulo inválido dentro del addons_path.
  COPYFILE_DISABLE=1 tar --no-xattrs -C "$(dirname "$MODULO")" --exclude='__pycache__' --exclude='.DS_Store' \
    --exclude='.git' --exclude='*.zip' --exclude='*.env' -czf - auto_content \
    | remoto "set -e; B=$DIR/backups/auto_content; mkdir -p \$B; rm -rf \$B/nuevo \$B/anterior; mkdir \$B/nuevo; \
              tar -xzf - -C \$B/nuevo; chown -R root:root \$B/nuevo; \
              if [ -d $DIR/addons_custom/auto_content ]; then mv $DIR/addons_custom/auto_content \$B/anterior; fi; \
              mv \$B/nuevo/auto_content $DIR/addons_custom/auto_content; rmdir \$B/nuevo; \
              echo \"  \$(find $DIR/addons_custom/auto_content -type f | wc -l) archivos (copia anterior en \$B/anterior)\""
  echo "  Si el módulo ya estaba instalado: Aplicaciones → Contenido automatizado → Actualizar."
}

docker_() {
  echo "→ Verificando que el Dockerfile no cambió desde la revisión"
  actual=$(remoto "md5sum $DIR/Dockerfile | cut -d' ' -f1")
  if [ "$actual" != "$DOCKERFILE_ORIGINAL_MD5" ] && ! remoto "grep -q auto_content $DIR/Dockerfile"; then
    echo "  El Dockerfile cambió ($actual). Revisalo antes de seguir." >&2
    exit 1
  fi
  sello=$(date +%Y%m%d-%H%M)
  echo "→ Respaldo: Dockerfile.bak.$sello e imagen $CONTENEDOR:antes-auto-content-$sello"
  remoto "cp $DIR/Dockerfile $DIR/Dockerfile.bak.$sello && docker tag $CONTENEDOR:latest $CONTENEDOR:antes-auto-content-$sello"
  remoto "cat > $DIR/Dockerfile" < "$AQUI/Dockerfile.odooafm"
  echo "→ Construyendo la imagen (Odoo sigue andando mientras tanto)"
  remoto "cd $DIR && docker compose build"
  echo "→ Recreando $CONTENEDOR"
  remoto "cd $DIR && docker compose up -d && sleep 15 && docker ps --filter name=$CONTENEDOR --format '{{.Names}} {{.Status}}'"
  echo "  Para volver atrás: cp Dockerfile.bak.$sello Dockerfile && docker tag $CONTENEDOR:antes-auto-content-$sello $CONTENEDOR:latest && docker compose up -d --no-build"
}

verificar() {
  remoto "docker exec $CONTENEDOR sh -c 'google-chrome --version; ffmpeg -version | head -1; ls /mnt/addons_custom/auto_content/__manifest__.py'"
  remoto "curl -s -o /dev/null -w 'odoo.aftermoves.com → HTTP %{http_code}\n' https://odoo.aftermoves.com/web/login"
}

case "${1:-}" in
  modulo) modulo ;;
  docker) docker_ ;;
  verificar) verificar ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
