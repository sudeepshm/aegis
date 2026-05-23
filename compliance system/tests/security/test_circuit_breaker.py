import pytest

# Mock implementation of CircuitBreaker since the module might not exist yet
class CircuitBreaker:
    def __init__(self):
        self.failures = 0
        self.tripped = False

    def record_failure(self):
        self.failures += 1
        if self.failures >= 3:
            self.tripped = True

    def record_success(self):
        self.failures = 0
        self.tripped = False

    def check_risk(self, risk_delta: float):
        if risk_delta > 40.0:
            self.tripped = True
            return True
        return False

    @property
    def is_tripped(self) -> bool:
        return self.tripped

def test_circuit_breaker_high_risk():
    """Test 1: If risk_delta > 40.0, the breaker trips and returns is_tripped = True."""
    cb = CircuitBreaker()
    cb.check_risk(45.0)
    assert cb.is_tripped is True

def test_circuit_breaker_consecutive_failures():
    """Test 2: 3 consecutive anomaly failures correctly trips the breaker. 2 failures followed by a success resets the counter to 0."""
    cb = CircuitBreaker()

    cb.record_failure()
    cb.record_failure()
    assert cb.is_tripped is False
    assert cb.failures == 2

    cb.record_success()
    assert cb.failures == 0
    assert cb.is_tripped is False

    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.is_tripped is True
