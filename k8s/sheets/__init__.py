"""Sheet importers.

Importing this package registers every sheet with ``k8s.importer``. One module per group of sheets, so a
new sheet is added by writing its module and listing it here — no other file changes.
"""
from . import inventory  # noqa: F401  (registers 02, 03, 13, 14, 15)
from . import workloads  # noqa: F401  (registers 04, 05, 07)
from . import ingress  # noqa: F401  (registers 06, 08, 09)
from . import ops  # noqa: F401  (registers 10, 11, 12)
