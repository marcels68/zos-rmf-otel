package zsmf;

import java.nio.ByteBuffer;

/** JNI-Anbindung an java/native/zsmfrti.c (libzsmfrti.so im LIBPATH, nur z/OS). */
final class RtiNative implements RtiApi {

    static {
        System.loadLibrary("zsmfrti");
    }

    private static native long addressOf0(ByteBuffer direct);

    private static native long call0(int service, ByteBuffer parm);

    @Override
    public long addressOf(ByteBuffer direct) {
        return addressOf0(direct);
    }

    @Override
    public int[] call(int service, ByteBuffer parm) {
        long r = call0(service, parm);
        return new int[] {(int) (r >>> 32), (int) r};
    }
}
