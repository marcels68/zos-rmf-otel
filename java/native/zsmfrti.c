/*
 * zsmfrti.c - JNI-Anbindung an das SMF Real-Time Interface (z/OS, AMODE 64).
 *
 * Nur zwei Funktionen; alle Parameterblöcke baut Java (zsmf.InMemorySource):
 *   addressOf0(ByteBuffer)      Adresse eines Direct-ByteBuffers
 *   call0(service, ByteBuffer)  IFAMCON (4) / IFAMGET (8) / IFAMDSC (12) aufrufen,
 *                               Ergebnis (rc << 32) | rsn
 *
 * Einsprungadresse über die CVT-Kette, wie in IBMs Doku ("alternative method"):
 *   LLGT 15,16(0,0)  CVT;  LLGT 15,196(,15);  L 15,376(,15);  LLGT 15,180(,15);
 *   L 15,off(,15)    off = 4 IFAMCON, 8 IFAMGET, 12 IFAMDSC
 * Aufruf mit Standard-OS-Linkage in AMODE 64 (CALL unter SYSSTATE AMODE64(YES)):
 *   R1 -> Parameterliste aus drei 8-Byte-Adressen (Block, rc, rsn), R13 -> genullter
 *   Sicherungsbereich (>= 144 Byte, Format 4), R14 = Rücksprung, R15 = Einsprung. Der
 *   Dienst stellt R2-R14 wieder her; R0, R1, R15 sind danach undefiniert.
 *
 * jni.h wird nicht gebraucht: aus JNI wird nur GetDirectBufferAddress verwendet, laut
 * JNI-Spezifikation Index 230 der Funktionstabelle (java/native/check-jni-index.c prüft
 * das gegen ein echtes jni.h). Mit -DUSE_JNI_H wird stattdessen jni.h eingebunden.
 *
 * Übersetzen: java/native/build-zos.sh (clang für s390x-ibm-zos, XPLINK-64, DLL).
 * Auf anderen Systemen (Test) liefert call0 RC X'10' statt eines Dienstaufrufs.
 */
#include <stdint.h>

#ifdef USE_JNI_H
#include <jni.h>
static void *direct_address(JNIEnv *env, jobject buf)
{
    return (*env)->GetDirectBufferAddress(env, buf);
}
#else
typedef int64_t jlong;
typedef int32_t jint;
typedef void *jobject;
typedef void *jclass;
typedef const void *const *JNIEnv;             /* zeigt auf die JNI-Funktionstabelle */
#define JNI_GET_DIRECT_BUFFER_ADDRESS 230
#define JNIEXPORT __attribute__((visibility("default")))
#define JNICALL
typedef void *(*get_direct_buffer_address_fn)(JNIEnv *, jobject);
static void *direct_address(JNIEnv *env, jobject buf)
{
    get_direct_buffer_address_fn f =
        (get_direct_buffer_address_fn)(*env)[JNI_GET_DIRECT_BUFFER_ADDRESS];
    return f(env, buf);
}
#endif

#ifdef __MVS__
/* Speicherwort an einer 31-Bit-Adresse lesen */
static uint32_t word(uint32_t addr)
{
    return *(volatile uint32_t *)(uintptr_t)addr;
}

static void *service_address(int off)
{
    uint32_t p = word(16);                 /* LLGT 15,16(0,0)   CVTPTR */
    p = word(p + 196) & 0x7FFFFFFFu;       /* LLGT 15,196(,15) */
    p = word(p + 376);                     /* L    15,376(,15) */
    p = word(p + 180) & 0x7FFFFFFFu;       /* LLGT 15,180(,15) */
    p = word(p + (uint32_t)off);           /* L    15,off(,15)  Einsprung */
    return (void *)(uintptr_t)(p & 0x7FFFFFFFu);
}

/* Aufruf mit OS-Linkage; R13 zeigt auf einen eigenen Sicherungsbereich. */
static void call_os(void *entry, void *plist, void *savearea)
{
    register void *r1 __asm__("1") = plist;
    register void *r13 __asm__("13") = savearea;
    register void *r15 __asm__("15") = entry;
    __asm__ volatile(" BASR 14,15"         /* Spalte 1 = Marke (HLASM-Syntax) */
                     : "+r"(r1), "+r"(r15)
                     : "r"(r13)
                     : "0", "14", "cc", "memory");
}
#endif

JNIEXPORT jlong JNICALL Java_zsmf_RtiNative_addressOf0(JNIEnv *env, jclass cls, jobject buf)
{
    (void)cls;
    return (jlong)(uintptr_t)direct_address(env, buf);
}

JNIEXPORT jlong JNICALL Java_zsmf_RtiNative_call0(JNIEnv *env, jclass cls, jint service,
                                                  jobject parm)
{
    (void)cls;
    if (service != 4 && service != 8 && service != 12)
        return (jlong)((uint64_t)0x10u << 32);
    void *blk = direct_address(env, parm);
    if (blk == 0)
        return (jlong)(((uint64_t)0x08u << 32) | 0x0802u);   /* kein Direct-Buffer */
#ifdef __MVS__
    int32_t rc = 0, rsn = 0;
    uint64_t savearea[64] __attribute__((aligned(8)));       /* 512 Byte >= F4SA */
    void *plist[3];
    for (int i = 0; i < 64; i++)
        savearea[i] = 0;
    plist[0] = blk;
    plist[1] = &rc;
    plist[2] = &rsn;
    call_os(service_address(service), plist, savearea);
    return (jlong)(((uint64_t)(uint32_t)rc << 32) | (uint32_t)rsn);
#else
    return (jlong)((uint64_t)0x10u << 32);                  /* kein z/OS */
#endif
}
