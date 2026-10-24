"""Query H.264 NVDEC initialization on the first visible CUDA device.

Run inside the pinned SDK image with GPU 0 as the only exposed device.
This probe does not decode media or allocate a decoder.
"""

import ctypes


def main() -> int:
    cuda = ctypes.CDLL("libcuda.so.1")
    video = ctypes.CDLL("libnvcuvid.so.1")
    error_name = cuda.cuGetErrorName
    error_name.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
    error_name.restype = ctypes.c_int

    def call(library, name, argument_types, *arguments):
        function = getattr(library, name)
        function.argtypes = argument_types
        function.restype = ctypes.c_int
        status = function(*arguments)
        label = ctypes.c_char_p()
        error_name(status, ctypes.byref(label))
        print(name, status, label.value.decode() if label.value else "unknown", flush=True)
        return status

    uint = ctypes.c_uint
    integer = ctypes.c_int
    pointer = ctypes.c_void_p
    if call(cuda, "cuInit", [uint], 0):
        return 1
    count = integer()
    if call(cuda, "cuDeviceGetCount", [ctypes.POINTER(integer)], ctypes.byref(count)):
        return 1
    print("visible_devices", count.value, flush=True)
    if count.value != 1:
        print("Expected exactly one exposed device; refusing to select a GPU.", flush=True)
        return 1

    context = pointer()
    if call(
        cuda, "cuDevicePrimaryCtxRetain", [ctypes.POINTER(pointer), integer],
        ctypes.byref(context), 0,
    ):
        return 1
    pushed = False
    try:
        if call(cuda, "cuCtxPushCurrent_v2", [pointer], context):
            return 1
        pushed = True
        # CUVIDDECODECAPS starts with three 32-bit input fields: codec, chroma
        # and bit-depth-minus-eight. Reserve ample zeroed space for ABI output
        # and reserved fields; this probe does not interpret their layout.
        caps = (uint * 1024)()
        caps[0] = 4  # cudaVideoCodec_H264
        caps[1] = 1  # cudaVideoChromaFormat_420
        caps[2] = 0  # 8-bit input
        status = call(video, "cuvidGetDecoderCaps", [pointer], ctypes.cast(caps, pointer))
        return 0 if status == 0 else 1
    finally:
        if pushed:
            previous = pointer()
            call(cuda, "cuCtxPopCurrent_v2", [ctypes.POINTER(pointer)], ctypes.byref(previous))
        call(cuda, "cuDevicePrimaryCtxRelease_v2", [integer], 0)


if __name__ == "__main__":
    raise SystemExit(main())
