/* Prüft gegen ein echtes jni.h, dass GetDirectBufferAddress Index 230 der JNI-Tabelle ist
   (zsmfrti.c verwendet diesen Index ohne jni.h). cc -I$JAVA_HOME/include{,/linux} ... */
#include <jni.h>
#include <stddef.h>
#include <stdio.h>
int main(void)
{
    size_t i = offsetof(struct JNINativeInterface_, GetDirectBufferAddress) / sizeof(void *);
    printf("GetDirectBufferAddress = Index %zu\n", i);
    return i == 230 ? 0 : 1;
}
