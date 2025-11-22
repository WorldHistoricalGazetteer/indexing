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
        with zipfile.ZipFile(file_path, 'r') as zf:
            # Assume there is only one relevant file in the zip (like alternateNamesV2.txt)
            for name in zf.namelist():
                if name.endswith('.txt'):
                    with zf.open(name, 'r') as f:
                        for line in f:
                            yield line.decode('utf-8').strip()

    else:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                yield line.rstrip("\n")
