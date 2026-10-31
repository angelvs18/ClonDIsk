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

### Correcciones tras la primera migración real (HDD MBR → SSD GPT/UEFI → HP 260 G4)

- **Acceso controlado a carpetas**: se comprueba en el Resumen, antes de clonar (Get-MpPreference: modo 1 o 3 y el ejecutable actual fuera de las aplicaciones permitidas). Un aviso ofrece «Permitir ClonDisk» (Add-MpPreference, con confirmación) y la clonación no empieza hasta resolverlo o hasta que el usuario lo confirme. Se explica el aviso de bcdboot «BFSVC Warning: Failed to flush system partition. Error = [5]».
- **Actualizaciones pendientes** en el Windows de origen (pending.xml, RebootPending, SessionsPending, RebootRequired, PendingFileRenameOperations), leídas de copias de sus colmenas: aviso en el Resumen y en la pantalla final.
- **Tiempo estimado** como rango; cuenta «Verificar» (x2) y se ajusta con una lectura secuencial de ~256 MB del origen al analizar las particiones.
- Déficit de espacio en GB legibles («faltan 354 GB») y «Arranque: BIOS → UEFI (mbr2gpt al final)» en la página Particiones.
- **Letras temporales**: se registran y se quitan por letra (no por número de partición, que mbr2gpt cambia). Corrige «No se pudo quitar la letra Z:». Al terminar, el destino no conserva letras asignadas por ClonDisk.
- **«Preparar para desconectar el disco destino»** en la pantalla final: Write-VolumeCache por volumen, quita todas las letras y deja el disco offline.
- El registro muestra el estado de cada **servicio de almacenamiento** aunque no cambie («storahci: Start ya era 0, sin StartOverride»).
- **WinRE**: se localiza Winre.wim (partición de recuperación o Windows\System32\Recovery), se registra con `reagentc /setreimage /target` y se revisa ReAgent.xml. La comprobación final incluye «WinRE registrado» y la indicación de ejecutar `reagentc /enable` en el primer arranque (no se puede habilitar sin conexión).
- **Secure Boot**: se registra con qué certificado está firmado `bootmgfw.efi` y se avisa si es «Windows UEFI CA 2023», que los firmwares anteriores a 2023 rechazan con Secure Boot activo.
- **IP fijas** de la tarjeta de red anterior: se muestran al final y se guardan en el escritorio público del clon (`ClonDisk - red anterior.txt` y `ClonDisk-restaurar-red.ps1`).
- **Lista posterior a la migración** en el Resumen, en la pantalla final y en el README.

## 1.0.0

- Primera versión pública.
