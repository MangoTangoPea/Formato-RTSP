#!/bin/bash

# Obtener la ruta absoluta del directorio donde se encuentra este script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "========================================================"
echo " INICIANDO COMPILACION DE LIBREALSENSE PARA WSL (LIBUVC)"
echo "========================================================"
echo ""
echo "[1/7] Desinstalando versión de pip..."
cd "$SCRIPT_DIR"

# Detectar el entorno virtual activo o usar .venv/.venvv según exista
VENV_DIR=".venv"
if [ -d ".venvv" ]; then
    VENV_DIR=".venvv"
fi

if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
    pip uninstall -y pyrealsense2
else
    echo "Advertencia: No se encontró entorno virtual en $SCRIPT_DIR/$VENV_DIR."
fi

echo ""
echo "[2/7] Instalando dependencias de Ubuntu..."
sudo apt-get update
sudo apt-get install -y git cmake libssl-dev freeglut3-dev libusb-1.0-0-dev pkg-config libgtk-3-dev build-essential python3-dev python3-setuptools

echo ""
echo "[3/7] Descargando código fuente..."
cd ~
rm -rf librealsense
git clone https://github.com/IntelRealSense/librealsense.git
cd librealsense

echo ""
echo "[4/7] Configurando compilación con CMake..."
mkdir -p build && cd build
cmake ../ -DFORCE_LIBUVC=true -DBUILD_PYTHON_BINDINGS=bool:true -DPYTHON_EXECUTABLE="$SCRIPT_DIR/$VENV_DIR/bin/python3" -DCMAKE_BUILD_TYPE=release -DBUILD_EXAMPLES=false -DBUILD_GRAPHICAL_EXAMPLES=false

echo ""
echo "[5/7] Compilando (Esto tomará entre 15 y 30 minutos)..."
make -j$(nproc)

echo ""
echo "[6/7] Instalando en el sistema..."
sudo make install

echo ""
echo "[7/7] Copiando librerías al entorno virtual..."
cp wrappers/python/pyrealsense2*.so "$SCRIPT_DIR/$VENV_DIR/lib/python3.10/site-packages/" 2>/dev/null || cp wrappers/python/pyrealsense2*.so "$SCRIPT_DIR/$VENV_DIR/lib/python3.*/site-packages/"
cp wrappers/python/pybackend2*.so "$SCRIPT_DIR/$VENV_DIR/lib/python3.10/site-packages/" 2>/dev/null || cp wrappers/python/pybackend2*.so "$SCRIPT_DIR/$VENV_DIR/lib/python3.*/site-packages/"

echo ""
echo "========================================================"
echo " COMPILACION FINALIZADA CON EXITO"
echo "========================================================"
echo "Ya puedes ejecutar: sudo $SCRIPT_DIR/$VENV_DIR/bin/python3 emisor_ubuntu.py"
