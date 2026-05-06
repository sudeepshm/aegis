"""
tests/test_map_generator.py
============================
Unit tests for llm/map_generator.py — MAP generation pipeline.
"""

import pytest
from llm.map_generator import (
    RiskLevel, MAPStatus, Obligation, ActionItem, MAPDocument,
    SMERulesEngine, ObligationExtractor, GuardrailsValidator,
    MAPGenerator, CONFIDENCE_THRESHOLD,
)


class TestSMERulesEngine:
    def setup_method(self):
        self.engine = SMERulesEngine()

    def test_kyc_maps_to_operations(self):
        action = ActionItem(action_id="a1", obligation_id="o1", description="KYC verification")
        obligation = Obligation(obligation_id="o1", source_text="", regulation_ref="", description="KYC process")
        result = self.engine.apply(action, obligation)
        assert result.owner_dept == "Operations"

    def test_encryption_maps_to_it_security(self):
        action = ActionItem(action_id="a1", obligation_id="o1", description="Deploy encryption")
        obligation = Obligation(obligation_id="o1", source_text="", regulation_ref="", description="Encryption mandate")
        result = self.engine.apply(action, obligation)
        assert result.owner_dept == "IT Security"

    def test_mandatory_sets_high_risk(self):
        action = ActionItem(action_id="a1", obligation_id="o1", description="Mandatory task")
        obligation = Obligation(obligation_id="o1", source_text="", regulation_ref="", description="mandatory")
        result = self.engine.apply(action, obligation)
        assert result.risk_level == RiskLevel.HIGH
        assert result.deadline_days == 14

    def test_default_dept_is_compliance(self):
        action = ActionItem(action_id="a1", obligation_id="o1", description="Generic task")
        obligation = Obligation(obligation_id="o1", source_text="", regulation_ref="", description="Generic")
        result = self.engine.apply(action, obligation)
        assert result.owner_dept == "Compliance"


class TestObligationExtractor:
    def setup_method(self):
        self.extractor = ObligationExtractor(llm_client=None)

    @pytest.mark.asyncio
    async def test_extracts_shall_obligations(self):
        text = "Banks shall implement KYC norms. Banks shall report to RBI."
        obligations = await self.extractor.extract(text, "doc-001")
        assert len(obligations) >= 2

    @pytest.mark.asyncio
    async def test_no_obligations_in_plain_text(self):
        text = "The weather is sunny today."
        obligations = await self.extractor.extract(text, "doc-001")
        assert len(obligations) == 0


class TestGuardrailsValidator:
    def setup_method(self):
        self.validator = GuardrailsValidator()

    def test_validated_above_threshold(self):
        doc = MAPDocument(map_id="m1", doc_id="d1")
        doc.obligations = [Obligation("o1", "", "", "Test", confidence=0.90)]
        doc.action_items = [ActionItem("a1", "o1", "Action", owner_dept="Compliance", evidence_type="cert")]
        result = self.validator.validate(doc)
        assert result.status == MAPStatus.VALIDATED

    def test_escalated_below_threshold(self):
        doc = MAPDocument(map_id="m1", doc_id="d1")
        doc.obligations = [Obligation("o1", "", "", "Test", confidence=0.50)]
        doc.action_items = [ActionItem("a1", "o1", "Action", owner_dept="Compliance", evidence_type="cert")]
        result = self.validator.validate(doc)
        assert result.status == MAPStatus.ESCALATED
        assert result.escalated is True
        assert "Compliance Officer" in result.escalation_reason


class TestMAPGenerator:
    def setup_method(self):
        self.generator = MAPGenerator(llm_client=None)

    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        text = "Banks shall implement KYC norms. All systems must use AES-256 encryption."
        result = await self.generator.generate(doc_id="doc-001", text=text)
        assert result.map_id
        assert result.doc_id == "doc-001"
        assert len(result.obligations) >= 1
        assert result.status in (MAPStatus.VALIDATED, MAPStatus.ESCALATED, MAPStatus.FAILED)

    @pytest.mark.asyncio
    async def test_empty_text(self):
        result = await self.generator.generate(doc_id="doc-001", text="")
        assert len(result.obligations) == 0
