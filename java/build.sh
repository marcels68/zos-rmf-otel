#!/bin/sh
# Übersetzen, JAR bauen und testen (ohne Maven/Gradle, nur JDK 11+ und Python 3).
#   java/build.sh            -> java/build/zsmf-otel.jar, Tests, Gleichheitstest mit Python
#   java/build.sh --no-test  -> nur JAR
set -eu
cd "$(dirname "$0")"
rm -rf build
mkdir -p build/classes build/test
javac --release 11 -Xlint:all -Werror -encoding UTF-8 -d build/classes src/zsmf/*.java
printf 'Main-Class: zsmf.Main\n' > build/manifest.txt
jar --create --file build/zsmf-otel.jar --manifest build/manifest.txt -C build/classes .
echo "build/zsmf-otel.jar"
[ "${1:-}" = "--no-test" ] && exit 0
javac --release 11 -encoding UTF-8 -cp build/classes -d build/test test/zsmf/*.java
python3 ../smf/smf_gen.py build/test.bin --minutes 90 --seed 4 --end 2026-10-21T12:00 >/dev/null
java -Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8 -cp build/classes:build/test zsmf.Tests build/test.bin
# JNI-Teil auf dem Entwicklungsrechner prüfen (ohne Dienstaufruf), wenn ein C-Compiler da ist
if command -v cc >/dev/null 2>&1 && [ -n "${JAVA_HOME:-$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")}" ]; then
  JH=${JAVA_HOME:-$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")}
  OSINC=$(ls -d "$JH"/include/*/ 2>/dev/null | head -1)
  cc -I"$JH/include" -I"$OSINC" -o build/check-jni-index native/check-jni-index.c && build/check-jni-index
  cc -shared -fPIC -O2 -Wall -Wextra -Werror -o build/libzsmfrti.so native/zsmfrti.c
  java -Djava.library.path=build -cp build/classes:build/test zsmf.NativeSmoke
fi
python3 parity.py
