#!/usr/bin/env python3
"""
Receptor RTSP: se conecta a un servidor RTSP y muestra el flujo de vídeo
del mosaico RealSense D435 (4 streams) en tiempo real usando OpenCV.

Arquitectura de recepción RTSP:
───────────────────────────────
  ┌────────────────┐                ┌──────────┐   RTSP/RTP    ┌──────────┐
  │  RealSense     │ ─── FFmpeg ──► │ MediaMTX │ ────────────► │ Receptor │
  │  D435 (emisor) │                │ (server) │               │ (este    │
  └────────────────┘                └──────────┘               │  script) │
                                                               └──────────┘

Secuencia de señalización RTSP que realiza el receptor:
  1. DESCRIBE  → Solicita la descripción SDP (Session Description Protocol)
                  del flujo disponible en la URL RTSP.
  2. SETUP     → Negocia los parámetros de transporte RTP:
                  puertos UDP o TCP entrelazado para recibir paquetes.
  3. PLAY      → Inicia la recepción de paquetes RTP con vídeo H.264.
  4. TEARDOWN  → Finaliza la sesión cuando el receptor se desconecta.

OpenCV maneja toda esta secuencia internamente al abrir una URL rtsp://
con cv2.VideoCapture().

El fotograma recibido es el mosaico completo del emisor (1920x1440):
  ┌──────────────────────────────────────────────────────────┐
  │                                                          │
  │                   Color (RGB)                            │
  │                   1920 x 1080                            │
  │                                                          │
  ├──────────────────┬───────────────────┬───────────────────┤
  │    Infrared 1    │   Depth Heatmap   │    Infrared 2     │
  │    640 x 360     │     640 x 360     │     640 x 360     │
  └──────────────────┴───────────────────┴───────────────────┘

Uso:
    python receptor.py [URL_RTSP]
    python receptor.py [IP_EMISOR] [PUERTO]

Ejemplos:
    python receptor.py rtsp://[IP_ADDRESS]/camara
    python receptor.py [IP_ADDRESS]
"""

import sys
import time
import argparse
import os

# ─── Configurar la codificación de la consola para Unicode en Windows ─────
# Evita UnicodeEncodeError al imprimir caracteres como ═, ✓, ✗, ⚠, etc.
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# ─── Intentar importar OpenCV ─────────────────────────────────────────────
try:
    import cv2
except ImportError:
    print("Error: opencv-python no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)

# ─── Intentar importar numpy ──────────────────────────────────────────────
try:
    import numpy as np
except ImportError:
    print("Error: numpy no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES Y CONFIGURACIÓN POR DEFECTO
# ═══════════════════════════════════════════════════════════════════════════

# Puerto RTSP estándar (RFC 2326)
PUERTO_RTSP_DEFECTO = 8554

# Ruta del flujo en el servidor (debe coincidir con la del emisor)
RUTA_FLUJO = "camara"

# Tiempo de espera antes de reintentar la conexión (en segundos)
RETARDO_RECONEXION = 3

# Número máximo de intentos de reconexión (-1 = infinito)
MAX_REINTENTOS = -1

# Nombre de la ventana de visualización
NOMBRE_VENTANA = "Receptor RTSP — RealSense D435 (Mosaico 4 Streams)"


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIONES AUXILIARES
# ═══════════════════════════════════════════════════════════════════════════

def construir_url_rtsp(ip, puerto, ruta=RUTA_FLUJO):
    """
    Construye la URL RTSP completa a partir de los componentes.

    Formato de URL RTSP según RFC 2326:
      rtsp://<host>:<puerto>/<ruta>

    Donde:
      - host:   dirección IP o nombre de host del servidor RTSP
      - puerto: puerto TCP del servidor (por defecto 554 en RTSP,
                pero usamos 8554 para MediaMTX)
      - ruta:   identificador del recurso/flujo en el servidor

    Args:
        ip: Dirección IP del servidor RTSP
        puerto: Puerto TCP del servidor
        ruta: Ruta del recurso en el servidor

    Retorna la URL RTSP completa como cadena.
    """
    return f"rtsp://{ip}:{puerto}/{ruta}"


def configurar_captura_rtsp(url_rtsp):
    """
    Configura un objeto cv2.VideoCapture optimizado para RTSP.

    OpenCV usa internamente FFmpeg para la decodificación RTSP.
    Al abrir una URL rtsp://, FFmpeg realiza automáticamente la
    secuencia de señalización RTSP:
      1. Conecta al servidor por TCP en el puerto indicado
      2. Envía DESCRIBE para obtener la descripción SDP del flujo
      3. Envía SETUP para configurar el canal RTP
      4. Envía PLAY para comenzar a recibir paquetes RTP

    Las variables de entorno OPENCV_FFMPEG_* permiten personalizar
    el comportamiento del backend FFmpeg dentro de OpenCV:
      - OPENCV_FFMPEG_CAPTURE_OPTIONS: opciones adicionales para FFmpeg
        → "rtsp_transport;tcp" fuerza TCP en vez de UDP para el transporte
          RTP, lo cual es más fiable en redes con firewalls o NAT.

    Args:
        url_rtsp: URL RTSP completa del flujo a recibir.

    Retorna un objeto cv2.VideoCapture configurado, o None si falla.
    """
    # Configurar FFmpeg dentro de OpenCV para usar TCP como transporte RTSP
    # Esto evita problemas con firewalls que bloquean puertos UDP aleatorios
    # usados por RTP en modo UDP. TCP entrelazado (interleaved) encapsula
    # los paquetes RTP dentro de la misma conexión TCP de señalización.
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

    # Crear el objeto de captura con el backend FFmpeg
    # cv2.CAP_FFMPEG asegura que se use FFmpeg para manejar RTSP
    captura = cv2.VideoCapture(url_rtsp, cv2.CAP_FFMPEG)

    if not captura.isOpened():
        return None

    # Configurar el buffer de captura al mínimo para reducir latencia
    # Un buffer grande introduce retardo porque OpenCV almacena fotogramas
    # antes de entregarlos al programa
    captura.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    return captura


def mostrar_info_flujo(captura):
    """
    Muestra información técnica del flujo RTSP recibido.
    Lee las propiedades del vídeo desde el objeto VideoCapture
    que internamente las obtiene de la descripción SDP del flujo.

    Args:
        captura: Objeto cv2.VideoCapture conectado al flujo RTSP.
    """
    ancho = int(captura.get(cv2.CAP_PROP_FRAME_WIDTH))
    alto = int(captura.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = captura.get(cv2.CAP_PROP_FPS)

    print(f"  Resolución del mosaico: {ancho}x{alto}")
    print(f"  FPS del flujo: {fps:.1f}")

    # Informar la estructura del mosaico recibido
    print(f"  Contenido del mosaico:")
    print(f"    Fila superior: RGB (1920×1080)")
    print(f"    Fila inferior: IR1 (640×360) | Depth (640×360) | IR2 (640×360)")


def dibujar_hud(fotograma, fps_actual, fotogramas_recibidos, tiempo_transcurrido):
    """
    Dibuja un HUD (Head-Up Display) compacto con información de estado
    en la esquina inferior derecha del lienzo para evitar conflictos
    con los OSDs del emisor y el OSD nativo de la cámara RGB.

    A diferencia del emisor (que dibuja HUD sobre cada panel del mosaico),
    el receptor solo añade una barra de estado general para no duplicar
    la información ya visible en los OSD del emisor.

    Args:
        fotograma: Imagen OpenCV (numpy array BGR) donde dibujar.
        fps_actual: FPS calculado en tiempo real.
        fotogramas_recibidos: Contador total de fotogramas recibidos.
        tiempo_transcurrido: Segundos desde el inicio de la recepción.

    Retorna el fotograma con el HUD dibujado.
    """
    alto, ancho = fotograma.shape[:2]
    
    # Dimensiones de la barra de estado general
    box_ancho = 380
    box_alto = 80
    
    # Colocar en la esquina inferior derecha del mosaico (dentro del panel IR2)
    # dejando libre la esquina superior izquierda de cada panel.
    box_x2 = ancho - 10
    box_y2 = alto - 10
    box_x1 = box_x2 - box_ancho
    box_y1 = box_y2 - box_alto

    # ─── Fondo semitransparente para la barra de estado ───────────────
    overlay = fotograma.copy()
    cv2.rectangle(overlay, (box_x1, box_y1), (box_x2, box_y2), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.5, fotograma, 0.5, 0, fotograma)

    fuente = cv2.FONT_HERSHEY_SIMPLEX
    color_titulo = (0, 0, 255)   # Rojo para el título
    color_datos = (0, 255, 0)    # Verde para los datos

    # Línea 1: Título
    cv2.putText(fotograma, "RECEPTOR RTSP — Mosaico RealSense D435",
                (box_x1 + 10, box_y1 + 25), fuente, 0.5, color_titulo, 1, cv2.LINE_AA)

    # Línea 2: FPS y contador de frames
    cv2.putText(fotograma, f"FPS: {fps_actual:.1f} | Frames: {fotogramas_recibidos}",
                (box_x1 + 10, box_y1 + 50), fuente, 0.45, color_datos, 1, cv2.LINE_AA)

    # Línea 3: Tiempo transcurrido
    cv2.putText(fotograma, f"Tiempo: {tiempo_transcurrido:.0f}s | Presiona 'q' para salir",
                (box_x1 + 10, box_y1 + 70), fuente, 0.45, color_datos, 1, cv2.LINE_AA)

    return fotograma


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL RECEPTOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_receptor(url_rtsp, mostrar_hud_info=True):
    """
    Función principal del receptor RTSP.

    Se conecta a la URL RTSP indicada, recibe los paquetes RTP con
    vídeo H.264, los decodifica y muestra el mosaico completo (4 streams)
    en una ventana redimensionable de OpenCV.

    El fotograma recibido contiene el mosaico completo del emisor:
      - Fila superior: RGB (1920x1080)
      - Fila inferior: IR1 (640x360) | Depth heatmap (640x360) | IR2 (640x360)
      - Resolución total: 1920x1440

    NO se realiza ningún recorte ni división del frame. Se muestra
    exactamente como lo envía el emisor.

    Incluye lógica de reconexión automática: si el flujo se interrumpe
    (el emisor se detiene, la red falla, etc.), el receptor espera
    unos segundos y vuelve a intentar conectarse.

    Protocolo de red involucrado:
    ───────────────────────────
    - RTSP (TCP, puerto 8554): señalización de la sesión
      → OpenCV/FFmpeg envía DESCRIBE para obtener el SDP
      → Envía SETUP para negociar los parámetros RTP
      → Envía PLAY para iniciar la recepción de vídeo
    - RTP (TCP entrelazado): transporte de paquetes de vídeo H.264
      → Los paquetes llegan encapsulados dentro de la conexión TCP
      → FFmpeg los decodifica de H.264 a BGR24 para OpenCV
    - RTCP: informes de control de calidad del flujo
      → Se intercambian automáticamente por FFmpeg/OpenCV

    Args:
        url_rtsp: URL RTSP completa (e.g., rtsp://192.168.1.42:8554/camara)
        mostrar_hud_info: Si True, dibuja información de estado en el vídeo.
    """
    intento = 0

    print("\n" + "═" * 60)
    print("  RECEPTOR RTSP — RealSense D435 (Mosaico 4 Streams)")
    print("═" * 60)
    print(f"\n  URL RTSP: {url_rtsp}")
    print(f"  Transporte: TCP entrelazado (interleaved)")
    print(f"  Contenido: Mosaico completo (RGB + IR1 + Depth + IR2)")
    print(f"  Ventana: Redimensionable (cv2.WINDOW_NORMAL)")
    print(f"  Presiona 'q' en la ventana para salir.\n")

    while True:
        intento += 1
        captura = None

        # Verificar si se excedió el máximo de reintentos
        if MAX_REINTENTOS > 0 and intento > MAX_REINTENTOS:
            print(f"\n  ✗ Se alcanzó el máximo de {MAX_REINTENTOS} intentos.")
            break

        try:
            # ─── Intentar conectar al flujo RTSP ──────────────────────
            if intento == 1:
                print(f"  [Intento {intento}] Conectando a {url_rtsp} ...")
            else:
                print(f"  [Intento {intento}] Reconectando a {url_rtsp} ...")

            captura = configurar_captura_rtsp(url_rtsp)

            if captura is None or not captura.isOpened():
                # La conexión RTSP falló — el servidor no responde o la URL
                # no existe. Esto puede significar:
                #   - El emisor no está corriendo
                #   - La IP/puerto son incorrectos
                #   - El firewall bloquea la conexión TCP
                #   - La ruta del flujo no coincide
                print(f"  ✗ No se pudo conectar al flujo RTSP.")
                print(f"    Verificar que el emisor esté activo y la URL sea correcta.")
                print(f"    Reintentando en {RETARDO_RECONEXION} segundos ...")
                time.sleep(RETARDO_RECONEXION)
                continue

            # ─── Conexión exitosa ──────────────────────────────────────
            print(f"  ✓ Conectado al flujo RTSP")
            mostrar_info_flujo(captura)
            print(f"\n  Recibiendo vídeo en tiempo real ...\n")

            # Crear ventana redimensionable — 1920x1440 puede ser muy
            # grande para algunos monitores, WINDOW_NORMAL permite que
            # el usuario ajuste el tamaño de la ventana arrastrando bordes
            cv2.namedWindow(NOMBRE_VENTANA, cv2.WINDOW_NORMAL)

            # Ajustar el tamaño inicial de la ventana a algo razonable
            # para monitores estándar (escalar al 75% del original)
            cv2.resizeWindow(NOMBRE_VENTANA, 1440, 1080)

            # Contadores para estadísticas
            fotogramas_recibidos = 0
            tiempo_inicio = time.time()
            fotogramas_fallidos = 0

            # Cálculo de FPS optimizado: se actualiza cada N frames para no
            # sobrecargar el bucle de recepción con cálculos en cada fotograma.
            FPS_INTERVALO = 30
            fps_actual = 0.0
            fps_ultimo_tiempo = time.time()

            # ─── Bucle de recepción de fotogramas ──────────────────────
            # cv2.VideoCapture.read() internamente:
            #   1. Lee paquetes RTP del buffer de red
            #   2. Ensambla los fragmentos NAL del H.264
            #   3. Decodifica el fotograma H.264 a BGR24
            #   4. Retorna el fotograma como numpy array
            while True:
                ret, fotograma = captura.read()

                if not ret or fotograma is None:
                    # La lectura falló — posibles causas:
                    #   - El emisor dejó de enviar fotogramas
                    #   - Pérdida de paquetes RTP (corrupción)
                    #   - La conexión TCP se cerró
                    fotogramas_fallidos += 1

                    if fotogramas_fallidos > 30:
                        # Demasiados fotogramas fallidos consecutivos:
                        # el flujo probablemente se interrumpió
                        print("  ⚠ Flujo interrumpido (demasiados errores de lectura).")
                        break
                    continue

                # Resetear el contador de fallos al recibir un fotograma válido
                fotogramas_fallidos = 0
                fotogramas_recibidos += 1

                # Calcular FPS optimizado (cada 30 frames)
                if fotogramas_recibidos % FPS_INTERVALO == 0:
                    ahora = time.time()
                    delta = ahora - fps_ultimo_tiempo
                    if delta > 0:
                        fps_actual = FPS_INTERVALO / delta
                    fps_ultimo_tiempo = ahora

                tiempo_transcurrido = time.time() - tiempo_inicio

                # Dibujar información de estado (HUD) si está habilitado
                if mostrar_hud_info:
                    fotograma = dibujar_hud(
                        fotograma, fps_actual,
                        fotogramas_recibidos, tiempo_transcurrido
                    )

                # ─── Mostrar el fotograma completo en la ventana ───────
                # Se muestra el mosaico tal cual llega del emisor,
                # sin ningún recorte ni división del frame.
                cv2.imshow(NOMBRE_VENTANA, fotograma)

                # Verificar si el usuario presionó 'q' para salir
                # cv2.waitKey(1) espera 1ms y devuelve el código de tecla
                # El AND con 0xFF extrae los 8 bits menos significativos
                # (necesario para compatibilidad en algunos sistemas)
                tecla = cv2.waitKey(1) & 0xFF
                if tecla == ord('q') or tecla == ord('Q'):
                    print(f"\n  ⏹ Receptor detenido por el usuario (tecla 'q').")
                    print(f"    Fotogramas recibidos: {fotogramas_recibidos}")
                    print(f"    Tiempo total: {tiempo_transcurrido:.1f}s")
                    print(f"    FPS promedio: {fps_actual:.1f}")
                    # Liberar recursos y salir completamente
                    captura.release()
                    cv2.destroyAllWindows()
                    return

                # Si se cierra la ventana con la X, también salir
                if cv2.getWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_VISIBLE) < 1:
                    print(f"\n  ⏹ Ventana cerrada por el usuario.")
                    captura.release()
                    cv2.destroyAllWindows()
                    return

        except KeyboardInterrupt:
            # El usuario presionó Ctrl+C en la terminal
            print(f"\n\n  ⏹ Receptor detenido por el usuario (Ctrl+C).")
            break

        except cv2.error as e:
            # Error específico de OpenCV (decodificación, ventana, etc.)
            print(f"  ✗ Error de OpenCV: {e}")
            print(f"    Reintentando en {RETARDO_RECONEXION} segundos ...")
            time.sleep(RETARDO_RECONEXION)

        except Exception as e:
            # Cualquier otra excepción no prevista
            print(f"  ✗ Error inesperado: {e}")
            print(f"    Reintentando en {RETARDO_RECONEXION} segundos ...")
            time.sleep(RETARDO_RECONEXION)

        finally:
            # ─── Limpieza por iteración ────────────────────────────────
            # Liberar la captura actual antes de reintentar
            if captura is not None and captura.isOpened():
                captura.release()

    # ─── Limpieza final ────────────────────────────────────────────────
    cv2.destroyAllWindows()
    print("\n  Receptor finalizado.\n")


# ═══════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Receptor RTSP: recibe y muestra el mosaico de 4 streams de una Intel RealSense D435.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  python receptor.py rtsp://192.168.1.42:8554/camara      # URL completa
  python receptor.py 192.168.1.42                          # IP con puerto/ruta por defecto
  python receptor.py 192.168.1.42 8554                     # IP y puerto
  python receptor.py --sin-hud rtsp://192.168.1.42:8554/camara  # Sin overlay de info

Notas:
  - Asegúrate de que el emisor esté corriendo antes de iniciar el receptor.
  - Ambas máquinas deben estar en la misma red local.
  - Presiona 'q' en la ventana del vídeo para cerrar el receptor.
  - El mosaico recibido contiene: RGB + IR1 + Depth (heatmap) + IR2.
  - La ventana es redimensionable (arrastrar bordes para ajustar tamaño).
        """
    )

    parser.add_argument(
        "destino",
        nargs="?",
        default=None,
        help="URL RTSP completa (rtsp://IP:puerto/ruta) o dirección IP del emisor"
    )
    parser.add_argument(
        "puerto",
        nargs="?",
        type=int,
        default=PUERTO_RTSP_DEFECTO,
        help=f"Puerto del servidor RTSP (por defecto: {PUERTO_RTSP_DEFECTO})"
    )
    parser.add_argument(
        "--sin-hud",
        action="store_true",
        help="No mostrar información de estado (HUD) sobre el vídeo"
    )

    args = parser.parse_args()

    # Determinar la URL RTSP a partir de los argumentos
    if args.destino is None:
        # Sin argumentos: usar localhost por defecto
        url = construir_url_rtsp("127.0.0.1", PUERTO_RTSP_DEFECTO)
        print(f"  Sin argumentos. Usando URL por defecto: {url}")
        print(f"  Uso: python receptor.py <IP_EMISOR> [PUERTO]")
        print(f"       python receptor.py rtsp://IP:PUERTO/camara\n")
    elif args.destino.startswith("rtsp://"):
        # Se proporcionó una URL RTSP completa
        url = args.destino
    else:
        # Se proporcionó solo la IP (y opcionalmente el puerto)
        url = construir_url_rtsp(args.destino, args.puerto)

    # Iniciar el receptor
    iniciar_receptor(url, mostrar_hud_info=not args.sin_hud)
