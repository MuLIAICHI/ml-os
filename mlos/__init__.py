"""ml-os — read-only web viewer over the .ay/ state of registered ay-framework repos.

The .ay/ filesystem is the only seam: this package reads files from watched repos and
never writes into them. No database; no state beyond config (and, from task 2, a small
last-notified bookkeeping file).
"""
