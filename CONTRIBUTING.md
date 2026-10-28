# Cómo contribuir

1. Crea un entorno: `python -m venv .venv` y actívalo.
2. Instala en modo editable: `pip install -e .[dev]`.
3. Prueba tus cambios **sin tocar discos reales** con el modo laboratorio:
   `python -m clondisk --images origen.img destino.img`.
4. Ejecuta las pruebas (en Linux o WSL, con `gdisk`, `fdisk`, `ntfs-3g` y `dosfstools`):
   `python -m pytest tests -q`.
5. Abre un *pull request* describiendo qué cambia y cómo lo probaste.

Al reportar un fallo de clonación incluye: versión de Windows, tipo de tabla (GPT/MBR),
tamaño de sector de origen y destino, y el registro que muestra la ventana de progreso.
