# Cámara Telegram v1 — hp-server

Vigila las 3 cámaras Tapo de casa (1x C310 por cable + 2x C217 por Wi-Fi) y,
cuando la IA detecta **una persona o un animal (sobre todo gatos)**, manda
la foto por Telegram. **No graba vídeo.**

- Cada 5 minutos toma una foto de cada cámara por RTSP (stream2, la
  imagen secundaria, más ligera: el detector trabaja a 300x300, así que
  sobra).
- Primero compara con la foto anterior: si casi nada cambió, no hace nada.
  Esto filtra el movimiento de hojas y ramas sin gastar CPU.
- Si hubo movimiento, pasa la foto por MobileNet-SSD. Solo avisa por:
  persona, gato, perro, pájaro, caballo, oveja y vaca. Los árboles **no**
  son una clase del modelo, así que casi nunca provocan un aviso (ojo: las
  hojas y sombras con viento se pueden confundir a veces con pájaro).
- Guarda la foto en la carpeta `fotos/` con un máximo de **30 fotos**:
  al llegar a la 31, borra la más vieja. Nada se acumula en el disco.

> Nota: con una revisión cada 5 minutos, una persona que entra y sale
> entre dos fotos no aparece en ninguna. Es un compromiso para no cargar
> el servidor; la app Tapo ya graba por su cuenta. Si quieres más
> cobertura, baja `intervalo_segundos` a 120 en `config.json` y mide la
> carga antes de decidir.

Corre como servicio systemd en el servidor y arranca solo al prenderlo.

## Archivos

| Archivo | Qué es |
|---|---|
| `camara-telegram.py` | El servicio (un solo archivo, comentarios en español) |
| `config.json.example` | Plantilla de configuración (con lugares, sin claves reales) |
| `camara-telegram.service` | Unidad systemd |
| `README.md` | Esta guía |

## 1. Instalar dependencias en el servidor

```bash
sudo apt update
sudo apt install -y python3-opencv ffmpeg python3-requests git
```

`python3-opencv` trae OpenCV y NumPy; `ffmpeg` toma las fotos por RTSP;
`python3-requests` habla con Telegram.

## 2. Descargar el modelo de IA (MobileNet-SSD)

El modelo son dos archivos en la carpeta `modelo/`:

- `MobileNetSSD_deploy.prototxt` (la red)
- `MobileNetSSD_deploy.caffemodel` (los pesos entrenados)

> ⚠️ Ojo: los enlaces directos viejos de `wget` a estos dos archivos ya
> no funcionan (dan 404, verificado el 25 nov 2026). El repositorio
> original los genera y los pesos están en Google Drive. Pasos:

```bash
mkdir -p ~/camaras/modelo
cd ~/camaras

# 1) Clona el repositorio original (público, gratis)
git clone https://github.com/chuanqi305/MobileNet-SSD.git

# 2) Genera el archivo .prototxt de despliegue
cd MobileNet-SSD
bash gen_model.sh
# El archivo queda en example/MobileNetSSD_deploy.prototxt (compruébalo con):
find . -name "MobileNetSSD_deploy.prototxt"
cp example/MobileNetSSD_deploy.prototxt ~/camaras/modelo/
cd ..
```

```bash
# 3) Descarga los pesos (.caffemodel) desde Google Drive:
#    Abre https://github.com/chuanqi305/MobileNet-SSD en el navegador.
#    En la tabla del README hay dos enlaces de Drive: "train" y "deploy".
#    Abre el de "deploy" y descarga el archivo en ~/camaras/modelo/
#    con el nombre MobileNetSSD_deploy.caffemodel.
#
#    O desde la terminal con gdown (el ID es lo que va después de
#    /file/d/ en la URL de Drive):
pip install --break-system-packages gdown
gdown --id <ID_DEL_ENLACE_DEPLOY> -O ~/camaras/modelo/MobileNetSSD_deploy.caffemodel

# 4) Comprueba que están los dos:
ls -la ~/camaras/modelo/
```

## 3. Crear el bot de Telegram y conseguir el chat id

1. En Telegram busca **@BotFather**, pulsa `/newbot`, ponle un nombre
   (por ejemplo `RogerCamarasBot`) y un usuario que termine en `bot`.
   BotFather te da el **token**: algo como `123456789:AAExxxxxxxxx...`.
   Guárdalo, es la contraseña del bot: no la compartas con nadie.
2. Abre tu bot en Telegram y mándale un mensaje cualquiera (por ejemplo
   `hola`).
3. Consigue tu **chat id** de una de estas dos formas:
   - Habla con **@userinfobot**: te responde con tu id.
   - O abre en el navegador (cambia `<TOKEN>` por el tuyo):
     `https://api.telegram.org/bot<TOKEN>/getUpdates` y busca
     `"chat":{"id": 123456789}`. Ese número es tu chat id.

## 4. Activar RTSP en las cámaras Tapo

En la app Tapo, por cada cámara:

1. Ajustes de la cámara → Ajustes avanzados → **RTSP/ONVIF**
   (en algunas versiones: "Cuenta de cámara").
2. Crea un usuario y una contraseña **solo para la cámara**
   (no es tu cuenta TP-Link). Apúntalos: van en `config.json`.
3. La dirección RTSP queda así:
   `rtsp://usuario:contraseña@192.168.1.68:554/stream2`
   (`stream2` es la imagen secundaria, más ligera y suficiente para
   detectar, porque el modelo trabaja a 300x300; `stream1` es la imagen
   principal, en 2K en la C310, y gasta mucho más CPU. Comprueba que
   `stream2` abre en las tres cámaras; si alguna no lo abre, ponle
   `stream1` solo a esa en `config.json`).

Las cámaras ya tienen IP fija: **.68 la C310, .67 y .76 las C217**.

## 5. Configurar

```bash
cd ~/camaras
cp config.json.example config.json
chmod 600 config.json
nano config.json
```

Rellena:

- `telegram.token`: el token de BotFather.
- `telegram.chat_id`: tu chat id (número, sin comillas si es número;
  en el ejemplo va como texto, también vale).
- En cada cámara: `usuario` y `contrasena` de la cuenta RTSP.
- `nombre`: ponle el lugar real a cada C217 (por ejemplo
  "C217 - Entrada" y "C217 - Cochera") para reconocerlas en los avisos.
- `intervalo_segundos` (300), `cooldown_segundos` (60),
  `confianza_minima` (0.5), `max_fotos` (30): déjalos como están
  salvo que quieras afinar.

> 🔒 **Nunca subas `config.json` a GitHub ni lo pases por chat:**
> ahí van el token del bot y las contraseñas de las cámaras. Si subes
> este proyecto a GitHub, añade `config.json` al `.gitignore`. El
> `chmod 600` deja el archivo solo legible por tu usuario.
> También puedes poner el token con variables de entorno en el
> servicio (ver `camara-telegram.service`): crea
> `/etc/camara-telegram.env` con `TELEGRAM_BOT_TOKEN` y
> `TELEGRAM_CHAT_ID` y dale `sudo chmod 600`.

## 6. Instalar el servicio

```bash
cd ~/camaras
sudo cp camara-telegram.service /etc/systemd/system/camara-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable --now camara-telegram
systemctl status camara-telegram
```

Ver qué está haciendo (en vivo):

```bash
journalctl -u camara-telegram -f
```

Parar / arrancar / reiniciar:

```bash
sudo systemctl stop camara-telegram
sudo systemctl start camara-telegram
sudo systemctl restart camara-telegram
```

## 7. Probar antes de dejarlo corriendo

```bash
cd ~/camaras
# 1) ¿El bot responde? (te llega un mensaje de prueba)
python3 camara-telegram.py --probar-telegram
# 2) ¿El detector ve personas/animales en una foto?
python3 camara-telegram.py --probar-foto /ruta/a/una/foto.jpg
# 3) Una sola revisión de las 3 cámaras y se apaga (para probar RTSP)
python3 camara-telegram.py --una-vez
```

## Dónde quedan las fotos

En `~/camaras/fotos/`, con nombre `fecha_hora_camara_etiquetas.jpg`
(por ejemplo `2026-11-25_08-30-00-123_c310_persona-gato.jpg`). Como mucho
hay 30: al guardar la 31 se borra la más vieja automáticamente.

## Si algo falla

| Síntoma | Qué revisar |
|---|---|
| `no encontré los archivos del modelo` | Falta algo en `~/camaras/modelo/`. Repite el paso 2. |
| `falta ffmpeg` / `falta OpenCV` | Repite el paso 1. |
| `falta el token de Telegram o el chat id` | Revisa `config.json` (paso 5) o las variables de entorno. |
| `no pude tomar foto` una y otra vez | La cámara no responde por RTSP: revisa que la IP fija siga siendo la misma, que el usuario/contraseña RTSP sean los de la cuenta de cámara y que la cámara esté encendida. Prueba `stream2` si `stream1` no abre. |
| Llegan fotos de árboles | No debería pasar casi nunca (los árboles no son clase del modelo), pero las hojas y sombras con viento se pueden confundir a veces con pájaro. Si pasa, sube `confianza_minima` a 0.6 en `config.json`, o quita la clase pájaro si te llegan muchos falsos. |
| El servidor se siente lento | Sube `intervalo_segundos` a 600 y `cooldown_segundos` a 120 en `config.json`. La detección solo corre cuando hay movimiento, así que el gasto normal es bajo. |
| Avisos al amanecer o al anochecer | Normal: las cámaras pasan a visión nocturna (infrarrojo, blanco y negro) y eso genera movimiento falso una o dos veces al día. Solo añade carga puntual. |

## Ideas para después (no en v1)

- Usar la detección de personas que ya traen las C217 en vez de la IA
  del servidor (gastaría aún menos CPU).
- Avisar también por la app de Home Assistant cuando esté instalada.
- Un pequeño panel web con las últimas fotos, como el del monitor de red.
