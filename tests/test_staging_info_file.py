"""The staging info file must not overwrite a consuming job's SLURM_JOB_ID.

``/ix1/ishi/esinfo/es-staging.env`` is sourced by any batch job that needs to
talk to staging ES. It used to export ``SLURM_JOB_ID`` — the *staging* job's id
— which silently replaced the consuming job's own. Anything deriving a path
from it afterwards, and ``/scratch/slurm-$SLURM_JOB_ID`` is the standard CRC
idiom, then pointed at a directory owned by a different job:

    mkdir: cannot create directory '/scratch/slurm-23668874': Permission denied

That is the benign outcome. Had the staging job been on the same node, the job
would have written into another job's scratch instead of failing.

The canonical key is now ``STAGING_SLURM_JOB_ID``. ``SLURM_JOB_ID`` is still
written so an older ``es.sh`` can stop a running instance, but consumers must
prefer the explicit name.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class InfoFileWritesExplicitKey(unittest.TestCase):

    def setUp(self):
        self.sbatch = (REPO / "processing" / "es_staging.sbatch").read_text()

    def test_writes_staging_prefixed_key(self):
        self.assertIn("STAGING_SLURM_JOB_ID=$SLURM_JOB_ID", self.sbatch)

    def test_does_not_write_bare_slurm_job_id(self):
        """Writing it 'for compatibility' preserves the whole hazard.

        update_es.py derives its scratch path from SLURM_JOB_ID in Python, so a
        clobbered value reaches code that never mentions this file. es.sh keeps
        a fallback for info files written before the change, which is what
        preserves the ability to stop an already-running instance.
        """
        for line in self.sbatch.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotRegex(
                stripped, r"^SLURM_JOB_ID=",
                "the info file must not export a bare SLURM_JOB_ID")

    def test_documents_the_hazard_where_it_is_written(self):
        """The comment has to sit next to the line, or it will be lost."""
        idx = self.sbatch.index("STAGING_SLURM_JOB_ID=$SLURM_JOB_ID")
        window = self.sbatch[idx:idx + 700].lower()
        self.assertIn("replace", window,
                      "say what goes wrong, next to the line that governs it")
        self.assertIn("update_es", window,
                      "name the Python consumer — the hazard is not shell-only")

    def test_self_ownership_check_accepts_either_key(self):
        """The cleanup guard must still recognise its own info file."""
        self.assertIn("^(STAGING_)?SLURM_JOB_ID=", self.sbatch)


class ConsumersPreferTheExplicitKey(unittest.TestCase):

    def test_es_sh_prefers_staging_key(self):
        src = (REPO / "scripts" / "es.sh").read_text()
        self.assertIn('SLURM_JOB_ID="${STAGING_SLURM_JOB_ID:-$SLURM_JOB_ID}"',
                      src)

    def test_es_sh_unsets_both_on_stop(self):
        src = (REPO / "scripts" / "es.sh").read_text()
        self.assertRegex(
            src, r"unset [^\n]*\bSLURM_JOB_ID\b[^\n]*\bSTAGING_SLURM_JOB_ID\b")


class ScratchIsDerivedBeforeSourcing(unittest.TestCase):
    """Generated sbatch bodies must fix SCRATCH_DIR before sourcing staging env.

    symphonym.sh happens to do this already. Pinning it stops a later edit from
    reordering the two and reintroducing the fault silently.
    """

    def test_symphonym_sets_scratch_before_sourcing_staging(self):
        src = (REPO / "scripts" / "symphonym.sh").read_text()
        for match in re.finditer(r'source "\$\{?STAGING_INFO_FILE\}?"', src):
            preceding = src[:match.start()]
            # Within the same generated body, SCRATCH_DIR must already be set.
            last_scratch = preceding.rfind("SCRATCH_DIR=")
            last_heredoc = preceding.rfind("sbatch --parsable")
            if last_heredoc == -1:
                continue  # outer-shell source, not inside a job body
            if last_scratch > last_heredoc:
                continue  # ordered correctly
            # Otherwise the body must not use SLURM_JOB_ID after this point.
            following = src[match.end():match.end() + 1500]
            self.assertNotIn(
                "SLURM_JOB_ID", following,
                "a job body that sources the staging env must not use "
                "SLURM_JOB_ID afterwards — it is the staging job's id")


if __name__ == "__main__":
    unittest.main()
