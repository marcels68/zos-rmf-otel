#!/bin/sh
# libzsmfrti.so (JNI-DLL, XPLINK-64) für z/OS bauen. Zwei Wege:
#
# A) Auf z/OS mit IBM Open XL C/C++ (falls vorhanden):
#      java/native/build-zos.sh openxl
#
# B) Quer-Übersetzen mit clang (z. B. scripts/zos-cc aus zos-cobol-llvm oder ein clang mit
#    SystemZ-Ziel), dann auf z/OS binden:
#      java/native/build-zos.sh cross            # erzeugt build/zsmfrti.o (GOFF)
#      -> zsmfrti.o nach z/OS kopieren (binär!), dort:
#      java/native/build-zos.sh bind
#
# Kein jni.h nötig (siehe zsmfrti.c). Ziel arch10 (zEC12), wie die übrige Laufzeit.
set -eu
cd "$(dirname "$0")"
LE=${LE:-CEE}                      # HLQ der LE-Bibliotheken (SCEEBND2, SCEELIB)
case "${1:-}" in
  openxl)
    ibm-clang64 -O2 -march=arch10 -fvisibility=default -shared -o libzsmfrti.so zsmfrti.c
    ;;
  cross)
    mkdir -p ../build
    CC=${CC:-clang}
    $CC --target=s390x-ibm-zos -march=zEC12 -O2 -ffreestanding -fvisibility=default \
        -c -o ../build/zsmfrti.o zsmfrti.c
    echo "../build/zsmfrti.o"
    ;;
  bind)
    # Wie libcob.so in zos-cobol-llvm: DLL mit Sidedeck, LE-Bindebibliotheken
    ld -b AMODE=64 -b CASE=MIXED -b DYNAM=DLL -b REUS=RENT -b MAP -V \
       -x libzsmfrti.x -o libzsmfrti.so zsmfrti.o \
       -S "//'$LE.SCEEBND2'" "//'$LE.SCEELIB(CELQS003)'" > libzsmfrti.map 2>&1 \
      || { grep -E 'IEW2[0-9]+[ES]' libzsmfrti.map | head; exit 1; }
    echo "libzsmfrti.so (Exporte: $(grep -c 'Java_zsmf' libzsmfrti.x 2>/dev/null || echo '?'))"
    ;;
  *)
    sed -n '2,15p' "$0"; exit 2 ;;
esac
