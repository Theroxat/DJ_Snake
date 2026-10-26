"""Low-level file helpers shared by the modules that persist JSON under
DATA_DIR. Imports nothing from the package, so any layer may use it."""

import os
import tempfile


def write_atomic(path, data):
    """Write bytes to `path` through a temp file and os.replace(), so a crash
    never leaves a torn file. The temp file is created 0600 (mkstemp), and the
    result keeps that mode."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
