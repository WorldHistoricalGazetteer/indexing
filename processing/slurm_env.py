"""Shared Slurm sbatch preamble for jobs that import the conda environment.

Some ``htc`` nodes carry a ``/lib64/libstdc++.so.6`` (GLIBCXX to 3.4.29) older
than the conda env's ``libicuuc.so.75`` requires, and the loader prefers the
system copy. ``import sqlite3`` then dies with::

    ImportError: /lib64/libstdc++.so.6: version `GLIBCXX_3.4.30' not found

It is the node image rather than the environment, so it strikes only *some*
nodes — which is why it has been diagnosed three times independently since
31 August 2026 before anyone noticed a fix already existed.

**Why this lives in one place.** The same missing export produced two failures
of wildly different cost on the same day:

* ``submit_ccode_slurm`` — raised at import, was diagnosed in an hour, cost
  nothing but time.
* the ``un-final`` h3 chain — ``h3_stage`` swallowed the ImportError through
  three stacked ``except Exception`` blocks, silently built ``un``'s
  ``h3_cover`` from the convex hull instead of the real polygon, and killed the
  ccode tier-1 prefilter corpus-wide while every stage reported success.

The entire difference was whether the error was swallowed. Copies of a fix in
two of nine submitters is how the other seven stay broken, so callers take the
preamble from here and a new submitter inherits it.
"""

from __future__ import annotations

#: Prefer the conda env's own libstdc++ over the (older) system one. MUST come
#: after ``conda activate`` — above it ``$CONDA_PREFIX`` is unset and the export
#: silently expands to a harmless no-op, which looks correct in a diff and does
#: nothing at runtime.
CONDA_LIB_PRELOAD = 'export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH}"'

#: Fail in one second on an affected node rather than after hours of real work,
#: and with a legible message rather than an ImportError three frames deep.
SQLITE_PROBE = "python -c 'import sqlite3; print(\"sqlite3 ok\", sqlite3.sqlite_version)'"


def conda_lib_preamble() -> list[str]:
    """Lines to emit immediately after ``conda activate <env>``."""
    return [CONDA_LIB_PRELOAD, SQLITE_PROBE]
