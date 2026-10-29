# Contenido automatizado (`auto_content`) · Odoo 19

Convierte en piezas de marca las fotos y los videos que un operador le reenvía al WhatsApp de Odoo, y le devuelve la pieza por WhatsApp para aprobarla, pedir cambios o descartarla.

Es un módulo aparte y para varios clientes: depende de `mail`, `whatsapp` y `llm_connector`, y no de `creative_lab` ni de `whatsapp_ai_agents`. Cada cliente es un **paquete de marca** (.zip); en el código no hay nada de ningún cliente.

## Cómo funciona

1. **Solo escucha a los operadores.** Los mensajes de los números cargados en *Configuración → Operadores* los toma el módulo. A esos chats les apaga el agente de ventas de `whatsapp_ai_agents` (si está instalado). El resto de las conversaciones de WhatsApp siguen igual.
2. **Junta el material.** Fotos, videos y textos van a una solicitud abierta. Cuando pasan *Espera para juntar* segundos (90 por defecto) sin mensajes nuevos, la solicitud pasa a la cola. Lo reenviado cuenta siempre como material, nunca como una orden.
3. **Arma la pieza.** Una tarea automática que corre cada minuto:
   - le manda a la IA las fotos (o un cuadro de cada video) y los textos, junto con las instrucciones, el conocimiento y los lugares de la marca;
   - la IA devuelve un JSON con el diseño, el archivo elegido, los textos y las alertas;
   - Chrome headless rellena la plantilla del paquete;
   - si el archivo es un video, ffmpeg le superpone la plantilla y lo recorta a 60 s.
4. **Se la devuelve al operador:**
   - la vista previa;
   - el archivo en calidad original (como documento);
   - un mensaje con el resumen, las alertas y los botones **Aprobar · Cambiar · Descartar**.
   Con **Cambiar**, el siguiente texto es el pedido de cambio y se arma una versión nueva. También funciona respondiendo (citando) la propuesta.
5. **Aprobar** deja la versión lista para publicar. La publicación en Instagram es la etapa 2.

**Comandos por WhatsApp:**
- `#codigo`: cambia la marca activa (por ejemplo, `#capsic`).
- `ayuda`: muestra la ayuda.
- `reintentar`: vuelve a intentar la última solicitud que falló.
- `ok` / `descartar`: equivalen a los botones.

## Requisitos del servidor

Hacen falta **Google Chrome** (o Chromium) y **ffmpeg** dentro del contenedor. `tools/Dockerfile.odooafm` los agrega:
- Chrome se instala desde el `.deb` oficial, no por pip, para no cambiar `greenlet`/`gevent`.
- Si están en otra ruta, configurá los parámetros del sistema `auto_content.chrome_path` y `auto_content.ffmpeg_path`.

Despliegue en odooafm: `tools/desplegar_odooafm.sh modulo`, después `docker` y `verificar`.

## Configuración

1. **Instalar** el módulo (Aplicaciones → *Contenido automatizado*).
2. **Proveedor de IA:** en LLM Connector tiene que haber uno de tipo **OpenAI** o **Anthropic**, con un modelo que vea imágenes. LLM Connector todavía no le manda imágenes a Gemini.
3. **Marca** (*Contenido automatizado → Configuración → Marcas*):
   1. Subí el paquete `.zip`.
   2. Elegí el proveedor y el modelo (por ejemplo, `gpt-4o`).
   3. Pulsá **Probar con la foto del paquete**: arma una pieza sin usar WhatsApp.
4. **Operador** (*Configuración → Operadores*):
   1. Cargá el número desde el que se reenvía, la cuenta de WhatsApp de Odoo y la marca activa.
   2. **Enviar mensaje de prueba** confirma el envío. WhatsApp solo deja escribirle si el operador mandó algo en las últimas 24 h.

## Paquete de marca

Es una carpeta con un `marca.json` en la raíz. Se arma con:

```bash
python3 tools/armar_paquete.py /ruta/a/la/carpeta-de-marca    # → <codigo>-paquete.zip
```

Contenido de `marca.json`:

| Clave | Para qué |
|---|---|
| `nombre`, `codigo` | Nombre visible y código para `#codigo` |
| `plantilla`, `ancho`, `alto` | El HTML de la pieza y su tamaño en px. Recibe los datos como JSON entre `/*DATOS*/` y `/*FIN*/` (más `plantilla` con el diseño elegido, `foto` como URL `file://` y `transparente: true` cuando es capa de video) |
| `disenos` | Los diseños que puede elegir la IA: `descripcion`, `media` (`requerida`, `opcional` o `no`) y `campos` |
| `instrucciones`, `conocimiento` | Los `.md` que lee la IA: cómo escribir y datos verificados |
| `lugares` | Nombres oficiales y sus variantes (`alias`); se cargan como registros editables |
| `foto_prueba`, `texto_prueba` | Para el botón de prueba |
| `incluir` | Qué archivos entran al .zip (opcional) |

Para que una plantilla sirva con videos, cuando recibe `transparente: true` tiene que dejar transparente el fondo de `html`, `body` y del contenedor, y no pedir imagen si `foto` viene vacía.

## Probar sin Odoo

```bash
python3 tools/probar_servicios.py <paquete.zip> /tmp/salida [video.mp4]
```

Valida el paquete, el prompt y el JSON de la IA, y renderiza una foto, la capa transparente y un aviso con el Chrome local. Si le pasás un video, también lo compone.
