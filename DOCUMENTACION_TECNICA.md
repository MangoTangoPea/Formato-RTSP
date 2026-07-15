# DOCUMENTACIÓN TÉCNICA: ESTEGANOGRAFÍA LSB Y GRABACIÓN CRÍTICA EN MOSAICO

Este documento detalla la arquitectura de software, fundamentos matemáticos y configuraciones de red de la versión 4 de la suite RTSP para Intel RealSense D435.

---

## 1. Fundamentos Matemáticos de la Esteganografía LSB con Redundancia

La esteganografía LSB (Least Significant Bit) es una técnica para ocultar datos modificando el bit menos significativo de los píxeles de una imagen. En nuestro sistema, cada frame transporta 128 bits de metadatos críticos:
*   **Frame ID** (64 bits): Entero secuencial que inicia en 1, utilizado para verificar caídas físicas de frames y sincronía temporal relativa.
*   **Timestamp Emisor** (64 bits): Marca de tiempo del reloj local del emisor en nanosegundos (`time.time_ns()`), permitiendo calcular latencia de red exacta en tiempo real.

```
Metadatos: [ Frame ID (64 bits) ] + [ Timestamp (64 bits) ] = 128 bits totales
```

### 1.1 Esquema de redundancia y robustez ante H.264
La compresión de video con pérdida (como H.264 / AVC) descarta frecuencias espaciales altas y aplica cuantización en bloques, lo que destruye los bits individuales si se inyectan en píxeles aislados. Para resolver esto, implementamos un esquema de redundancia física por repetición espacial:

*   Cada bit lógico del payload de 128 bits se expande a **8 píxeles físicos contiguos** (un bloque).
*   Por ende, inyectar 128 bits de datos requiere exactamente $128 \times 8 = 1024$ píxeles en la primera fila (fila 0) de la imagen.

```
Bit Lógico 0  ──►  [P0, P1, P2, P3, P4, P5, P6, P7] (LSBs iguales a 0)
Bit Lógico 1  ──►  [P8, P9, P10, P11, P12, P13, P14, P15] (LSBs iguales a 1)
```

### 1.2 Extracción mediante Votación por Mayoría (Majority Voting)
Al recibir un fotograma en el receptor, se lee el bit menos significativo de los 1024 píxeles correspondientes y se dividen en 128 bloques de 8 píxeles. Para cada bloque, se realiza una suma matemática:
$$S_i = \sum_{j=0}^{7} \text{LSB}(P_{8i + j})$$

El bit lógico resultante $B_i$ se decide según:
$$B_i = \begin{cases} 1 & \text{si } S_i > 4 \\ 0 & \text{si } S_i \le 4 \end{cases}$$

Este filtro estadístico actúa como un decodificador de corrección de errores por repetición. Permite reconstruir con total exactitud el Frame ID y el Timestamp original aun si la compresión de red alteró hasta 3 píxeles dentro de un mismo bloque de 8.

### 1.3 Visibilidad e Imperceptibilidad Físico-Óptica
Modificar únicamente el bit menos significativo altera el valor digital de un píxel en un delta de:
$$\Delta = \pm 1$$

En un espacio de color digital de 8 bits por canal ($2^8 = 256$ niveles de luminancia, 0 a 255):
*   La desviación relativa máxima inducida es de:
    $$\frac{1}{256} \approx 0.39\%$$
*   La inyección en imágenes BGR se realiza exclusivamente en el canal **Azul [0]** (el canal al que el ojo humano es ópticamente menos sensible en términos de resolución espacial y detalle de luminancia).
*   En imágenes monocromáticas (IR), la inyección se realiza de manera uniforme en la fila 0.
*   El impacto estético visual es imperceptible a distancias convencionales y pantallas de visualización.

---

## 2. Resiliencia del Contenedor Matroska (.mkv) ante Fallos de Energía

En sistemas de grabación crítica y flujos industriales, las interrupciones de energía o bloqueos del kernel representan un alto riesgo de corrupción de datos. 

| Contenedor | Estructura de Cabecera / Metadatos | Efecto ante Corte Eléctrico |
| :--- | :--- | :--- |
| **MP4 / MOV** | Requiere escribir el bloque de metadatos `moov` al final del archivo (contiene el índice de duraciones y punteros de cuadros). | **Corrupción Total:** El archivo resulta ilegible sin herramientas forenses complejas, ya que la cabecera no se cerró. |
| **Matroska (MKV)** | Estructurado en bloques binarios independientes agrupados en **Clusters** basados en el formato EBML (Extensible Binary Meta Language). | **Resiliencia Total:** El archivo se conserva utilizable y reproducible hasta el último milisegundo antes de la interrupción. |

### 2.1 Mosaico Físico Unificado contra Desfases
En grabaciones multi-stream (donde cada cámara es una pista de video separada), la pérdida selectiva de fotogramas (frame drops) en la red local desincroniza las pistas. 
Al utilizar un **mosaico de una sola vista físico de 1920×1440**:
1.  Los frames de las 4 cámaras se ordenan matricialmente en una sola imagen NumPy en memoria.
2.  El mosaico se envía a través de un único canal de comunicación (`stdin`).
3.  Cualquier caída de frames afectará de manera simétrica a las 4 fuentes, preservando la sincronía temporal relativa de manera inquebrantable.

---

## 3. Pipeline de Escritura y FFmpeg en Tiempo Real

El pipeline de transmisión y grabación se ha diseñado para evitar escrituras temporales en disco y llamadas a Named Pipes bloqueantes, unificando la arquitectura mediante el paso directo de descriptores de tuberías (`stdin` de procesos hijo en Python). Cada proceso FFmpeg actúa como servidor RTSP directo usando `-rtsp_flags listen`. La grabación del mosaico unificado ocurre en el receptor tras recibir los flujos de red:

```
   ┌────────────────────────────────────────────────────────┐
   │ Captura RealSense (wait_for_frames) -> Timestamp Único │
   └──────────────────────────┬─────────────────────────────┘
                              │
               [ Inyección LSB en 4 canales ]
                              │
                              ▼
                 ┌───────────────────────────┐
                 │ 4 FFmpeg (RTSP Server     │
                 │  directo, -rtsp_flags     │
                 │  listen)                  │
                 └────────────┬──────────────┘
                              │ (Transmisión RTSP directa)
                              ▼
                       ┌──────────────┐
                       │ Receptor OSD │
                       └──────┬───────┘
                              │
         ┌────────────────────┴─────────────────────┐
         │  Construcción Mosaico NumPy BGR 1920x1440 │
         └────────────────────┬─────────────────────┘
                              │
                       [ Write raw bytes ]
                              ▼
                 ┌───────────────────────────┐
                 │ stdin Proceso Hijo FFmpeg │
                 └─────────────┬─────────────┘
                               │
                [ Codificación libx264 preset ultrafast ]
                               ▼
                  ┌─────────────────────────┐
                  │  Archivo Matroska .mkv  │
                  └─────────────────────────┘
```

### 3.1 Comando FFmpeg para Escritura del Mosaico
El comando que ejecuta el receptor en segundo plano de manera síncrona es:

```bash
ffmpeg -y -f rawvideo -pix_fmt bgr24 -s 1920x1440 -r 30 -i - \
  -c:v libx264 -pix_fmt yuv420p -preset ultrafast -crf 18 \
  -metadata title="RealSense D435 Mosaico LSB (Receptor)" -f matroska grabacion.mkv
```

*   `-f rawvideo -pix_fmt bgr24 -s 1920x1440 -r 30 -i -`: Configura el canal de entrada del receptor para aceptar bytes de imagen crudos de OpenCV a 30 FPS.
*   `-c:v libx264 -preset ultrafast -crf 18`: Habilita codificación H.264 eficiente de baja latencia con factor de calidad constante (CRF) 18, garantizando que los bits LSB inyectados sobrevivan la codificación sin pérdidas perceptibles de datos.
*   `-f matroska`: Emite directamente al contenedor resiliente Matroska en la máquina receptora.

---

## 4. Guía de Ejecución Rápida

### 4.1 Máquina Emisora (Ubuntu con Intel RealSense D435)

#### Requisitos del Sistema
Instalar dependencias del sistema:
```bash
sudo apt update && sudo apt install -y ffmpeg python3-pip python3-venv libgl1 libglib2.0-0
```

#### Configurar Reglas Udev (Solo la primera vez)
```bash
wget https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules
sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```
*Desconectar y reconectar la cámara posterior a este comando.*

#### Ejecución del Emisor
1.  **Ejecución General:**
    ```bash
    python3 emisor_ubuntu.py
    ```
2.  **Ejecutar Diagnóstico Completo:**
    ```bash
    python3 emisor_ubuntu.py --diagnostico
    ```

---

### 4.2 Máquina Receptora (Windows / Ubuntu)

#### Requisitos en Windows
Asegurar tener instalado Python 3.8+ y las dependencias vía PowerShell:
```powershell
pip install opencv-python numpy imageio-ffmpeg
```

#### Ejecución del Receptor
Conéctese apuntando a la dirección IP LAN de la máquina emisora:
```bash
# Windows
python receptor.py 192.168.1.42

# Ubuntu
python3 receptor_ubuntu.py 192.168.1.42
```

Si desea realizar la **grabación del mosaico unificado** en el receptor, utilice el argumento `--grabar` indicando la ruta del archivo MKV:
```bash
# Windows (graba en grabacion.mkv o en ruta específica)
python receptor.py --grabar C:\ruta\video.mkv 192.168.1.42

# Ubuntu (graba en grabacion.mkv o en ruta específica)
python3 receptor_ubuntu.py --grabar /ruta/video.mkv 192.168.1.42
```

Si el servidor utiliza un puerto diferente a `8554`, añada el argumento:
```bash
python receptor.py 192.168.1.42 9554
```

#### Controles de Teclado
*   `M`: Alternar a vista **Mosaico** (Color superior, secundarios inferiores).
*   `1`: Mostrar únicamente **Color RGB (1920×1080)** a resolución nativa.
*   `2`: Mostrar únicamente **IR1 Left (1280×720)** a resolución nativa.
*   `3`: Mostrar únicamente **Depth Heatmap (1280×720)** a resolución nativa.
*   `4`: Mostrar únicamente **IR2 Right (1280×720)** a resolución nativa.
*   `H`: Ocultar / Mostrar HUD (auditoría visual de Frame ID y Latencias).
*   `F`: Alternar pantalla completa.
*   `Q` o `ESC`: Cerrar visor de red de forma limpia.
