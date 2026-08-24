"""Agent usage import adapters and their normalized intermediate representation.

The proxy has a C++ request/response IR.  Local agent logs are a separate
boundary, but they now use this small Python IR before being written to the
shared ``request_log`` table.
"""

from .ir import ParseBatch, UsageEvent, UsageSource

__all__ = ["ParseBatch", "UsageEvent", "UsageSource"]
