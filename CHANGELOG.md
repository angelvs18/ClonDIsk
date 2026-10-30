# Cambios

## 1.1.0

- **Versión del Windows de origen**: se lee ProductName, CurrentBuild y EditionID de una copia de su colmena SOFTWARE (el origen no se modifica) y se muestra en el asistente y en el resumen.
- **Respaldo de drivers** del Windows de origen (activado por defecto si no es el Windows en ejecución) con `dism /export-driver` en `Documentos\ClonDisk\drivers_<fecha>`, antes de reducir o clonar. Si DISM falla (p. ej. porque ese Windows es más nuevo que el anfitrión), se copian las carpetas del DriverStore que corresponden a los `oem*.inf`.
- **Reducir un Windows que no está en ejecución**: instrucciones correctas (ya no se recomienda `powercfg /h off`, que afectaba al anfitrión) y acción «Preparar volumen para reducir» (borra hiberfil/pagefile/swapfile y las instantáneas de ese volumen). El diálogo muestra el mínimo que permite Windows frente al tamaño necesario y avisa que se modifica el disco de ORIGEN.
- **Salud del origen** (HealthStatus y contadores de fiabilidad) antes de reducir y de clonar, con advertencia fuerte si hay errores de lectura no corregidos.
- **Sectores ilegibles**: reintentos dividiendo el bloque a la mitad hasta llegar a un sector (3 intentos por sector). Nueva opción «Saltar sectores ilegibles» (desactivada por defecto) que los copia con ceros y lista sus LBA; si no está activa, la copia se detiene indicando el LBA.
- **Convertir a GPT/UEFI** con `mbr2gpt` al terminar (orígenes MBR con Windows 10 o posterior, como máximo 3 particiones, anfitrión con Windows 10 1703 o posterior). Se reserva 1 MiB al final para la GPT de respaldo y MountedDevices se actualiza a los GUID nuevos.
- **«El SSD irá a otra computadora»**: habilita storahci, stornvme, iaStorV e iaStorAVC (Start=0, sin StartOverride) en el Windows clonado para evitar INACCESSIBLE_BOOT_DEVICE.
- `bcdboot` usa el del Windows clonado si su build es mayor que la del anfitrión.
- **Comprobación del arranque** al terminar (MBR 55AA, partición activa, bootmgr, BCD, hidden sectors o archivos EFI, y `bcdedit /enum {default}`), con ✓/✗ en la pantalla final.

## 1.0.0

- Primera versión pública.
