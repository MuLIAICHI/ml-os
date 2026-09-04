"""Test package for ml-os.

Quiet expected warning logs (e.g. the reader deliberately skipping a malformed .gate
file) so test output stays readable; failures still surface via assertions.
"""

import logging

logging.getLogger("mlos").setLevel(logging.ERROR)
