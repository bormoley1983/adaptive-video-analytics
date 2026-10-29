"""Explicit process-wide arithmetic policy owned by one GPU executor."""


def _targets(torch):
    return {
        "cuda_matmul": torch.backends.cuda.matmul,
        "cudnn": torch.backends.cudnn,
        "cudnn_conv": torch.backends.cudnn.conv,
    }


def precision_settings(torch):
    return {name: target.fp32_precision for name, target in _targets(torch).items()}


def restore_precision(torch, settings):
    for name, target in _targets(torch).items():
        target.fp32_precision = settings[name]


def configure_precision(torch):
    previous = precision_settings(torch)
    try:
        restore_precision(torch, dict.fromkeys(previous, "ieee"))
    except Exception:
        restore_precision(torch, previous)
        raise
    return previous
