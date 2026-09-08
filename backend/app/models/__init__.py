from app.models.balance import Balance
from app.models.accounting import AccountingEntry, AccountingTransaction
from app.models.accounting_proof import AccountingProofCheckpoint
from app.models.accounting_evidence_run import AccountingEvidenceRun
from app.models.admin_operation_audit import AdminOperationAudit
from app.models.contract_account import ContractAccount
from app.models.contract_adl_event import ContractAdlEvent
from app.models.contract_funding_event import ContractFundingEvent
from app.models.contract_funding_job import ContractFundingJob
from app.models.contract_funding_settlement import ContractFundingSettlement
from app.models.contract_insurance_event import ContractInsuranceEvent
from app.models.contract_insurance_fund import ContractInsuranceFund
from app.models.contract_ledger_entry import ContractLedgerEntry
from app.models.contract_liquidation_event import ContractLiquidationEvent
from app.models.contract_market_state import ContractMarketState
from app.models.contract_position import ContractPosition
from app.models.contract_risk_limit_tier import ContractRiskLimitTier
from app.models.contract_user_setting import ContractUserSetting
from app.models.causal_chain import CausalCommandJournal, CausalExecutionBundleRecord, CausalWatermark
from app.models.domain_event_log import DomainEventLog, DomainEventWatermark
from app.models.display_kline import DisplayKline
from app.models.exchange_runtime import ExchangeSnapshotRecord, ExchangeWatermark, JournalTruncationCheckpoint
from app.models.fee_profile import FeeProfile
from app.models.financial_outbox import (
    FinancialOutboxEvent,
    FinancialOutboxReplayRequest,
    OutboxCheckpoint,
    OutboxConsumerReceipt,
)
from app.models.kline import Kline
from app.models.ledger_entry import LedgerEntry
from app.models.market import Market
from app.models.market_bot_account import MarketBotAccount
from app.models.market_maker_instance import MarketMakerInstance
from app.models.market_strategy_config import MarketStrategyConfig
from app.models.order import Order
from app.models.reset_template import ResetTemplate
from app.models.strategy_template import StrategyTemplate
from app.models.trade import Trade
from app.models.user import User
from app.models.reconciliation_run import ReconciliationDifference, ReconciliationRun
from app.models.robot_financial_checkpoint import RobotFinancialCheckpoint, RobotFinancialCheckpointItem
from app.models.paper_exchange import (
    PaperAccountRun,
    PaperAsset,
    PaperBrandConfig,
    PaperLiquidityConfig,
    PaperGlobalRun,
    PaperResetRecord,
    PaperSession,
    PaperSystemSetting,
)

all_models = [
    AccountingEntry,
    AccountingEvidenceRun,
    AccountingProofCheckpoint,
    AccountingTransaction,
    AdminOperationAudit,
    Balance,
    ContractAccount,
    ContractAdlEvent,
    ContractFundingEvent,
    ContractFundingJob,
    ContractFundingSettlement,
    ContractInsuranceEvent,
    ContractInsuranceFund,
    ContractLedgerEntry,
    ContractLiquidationEvent,
    ContractMarketState,
    ContractPosition,
    ContractRiskLimitTier,
    ContractUserSetting,
    CausalCommandJournal,
    CausalExecutionBundleRecord,
    CausalWatermark,
    DomainEventLog,
    DomainEventWatermark,
    DisplayKline,
    ExchangeSnapshotRecord,
    ExchangeWatermark,
    FeeProfile,
    FinancialOutboxEvent,
    FinancialOutboxReplayRequest,
    Kline,
    LedgerEntry,
    Market,
    MarketBotAccount,
    MarketMakerInstance,
    MarketStrategyConfig,
    Order,
    OutboxCheckpoint,
    OutboxConsumerReceipt,
    ResetTemplate,
    StrategyTemplate,
    Trade,
    User,
    JournalTruncationCheckpoint,
    ReconciliationDifference,
    ReconciliationRun,
    RobotFinancialCheckpoint,
    RobotFinancialCheckpointItem,
    PaperAccountRun,
    PaperAsset,
    PaperBrandConfig,
    PaperLiquidityConfig,
    PaperSession,
    PaperSystemSetting,
    PaperGlobalRun,
    PaperResetRecord,
]
