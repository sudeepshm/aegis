import pytest
import datetime
from validation.intelligent_checks import ForgeryDetector

def test_forgery_detector_creation_date():
    """Test 1 (Forgery): Pass a mock PDF metadata dictionary where /CreationDate is from 2018 but the regulation year is 2026. Assert it returns FRAUD_SUSPECTED or HIGH risk."""

    pdf_bytes = b"%PDF-1.4\n/CreationDate (D:20180510143022+00'00')\n/Creator (Microsoft Word 2016)\n"

    detector = ForgeryDetector()
    claimed_date = datetime.date(2026, 5, 9)

    report = detector.detect(
        document_bytes=pdf_bytes,
        claimed_compliance_date=claimed_date,
        regulation_ref="REG-2026"
    )

    assert report.is_forged is True
    assert report.risk_level == "HIGH"
    assert any("CRITICAL" in f for f in report.findings)
    assert report.pdf_creation_date == "D:20180510143022+00'00'"
