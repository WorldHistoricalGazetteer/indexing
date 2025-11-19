import gzip
import zipfile


def stream_file(file_path):
    """
    Generator yielding lines from .txt, .gz, or .zip files.
    ZIP files are streamed without extraction.
    """
    if file_path.endswith(".gz"):
        with gzip.open(file_path, "rt", encoding="utf-8") as f:
            for line in f:
                yield line.rstrip("\n")

    elif file_path.endswith(".zip"):
        with zipfile.ZipFile(file_path) as z:
            # Expect exactly one .txt file inside Geonames ZIPs
            inner_name = [n for n in z.namelist() if n.endswith(".txt")][0]
            with z.open(inner_name, "r") as f:
                for line in f:
                    yield line.decode("utf-8").rstrip("\n")

    else:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                yield line.rstrip("\n")