"""Process-local matching stop-write latch; no I/O, scans, retries or locks.

One engine owns one latch, shared by its books, sequencers and ExchangeCore.
BusinessRejected is reserved for proven rejection/rollback, never arbitrary
ValueError/RuntimeError. A matcher rollback does not roll back DB or funds.
"""
class BusinessRejected(Exception):
    """Expected business rejection; caller must not hide uncertain execution."""
    status = "REJECTED"

class NotExecuted(BusinessRejected):
    """Admission failed before mutation; safe to report as not executed."""
    status = "NOT_EXECUTED"

class MatchingHalted(NotExecuted):
    pass

class ExecutionUnknown(RuntimeError):
    status = "UNKNOWN"

class BookInvariantError(ValueError):
    pass

class MatchingFault:
    __slots__ = ('halted', 'reason', 'category', 'revision')
    def __init__(self):
        self.halted = False
        self.reason = None
        self.category = None
        self.revision = 0

    def check(self):
        if self.halted:
            raise MatchingHalted(f'matching HALTED (instance): {self.reason}')

    def halt(self, reason, *, category='UNKNOWN'):
        self.revision += 1
        if not self.halted:
            self.reason = str(reason)
            self.category = category
        self.halted = True

    def snapshot(self):
        return {'halted': self.halted, 'scope': 'instance', 'reason': self.reason, 'category': self.category}
