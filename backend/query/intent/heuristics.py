"""Tier-1 Fast-Path Heuristic classification rules for sub-5ms intent routing."""

from dataclasses import dataclass, field
import re
from backend.query.types.intent import IntentLabel

# Department recognition patterns
DEPARTMENT_PATTERNS = {
    "HR": re.compile(r"\b(hr|nhân sự|phòng nhân sự|tuyển dụng|human resources)\b", re.IGNORECASE),
    "ENGINEERING": re.compile(r"\b(engineering|kỹ thuật|khối kỹ thuật|phát triển phần mềm|it|công nghệ|tech)\b", re.IGNORECASE),
    "SALES": re.compile(r"\b(sales|kinh doanh|khối kinh doanh|bán hàng)\b", re.IGNORECASE),
    "MARKETING": re.compile(r"\b(marketing|truyền thông|quảng cáo|pr)\b", re.IGNORECASE),
    "FINANCE": re.compile(r"\b(finance|tài chính|kế toán|ngân sách|accounting)\b", re.IGNORECASE),
    "LEGAL": re.compile(r"\b(legal|pháp chế|luật|pháp lý)\b", re.IGNORECASE),
}

# 1. GENERAL_CHAT regex: greetings, thanks, pleasantries, bot self-inquiry
GENERAL_CHAT_EXACT = re.compile(
    r"\b("
    r"xin chào|chào bạn|chào bot|chào em|chào anh|chào chị|chào buổi sáng|chào buổi chiều|chào buổi tối|hi|hello|hey|"
    r"cảm ơn|cảm ơn bạn|cảm ơn bot|cảm ơn nhiều|cảm ơn rất nhiều|cám ơn|thanks|thank you|"
    r"bạn là ai|bạn có thể làm gì|bạn tên là gì|tên của bạn là gì|"
    r"who are you|what can you do|what is your name|what's your name|"
    r"tạm biệt|bye|goodbye|see you|hẹn gặp lại|"
    r"chúc một ngày|chúc ngày mới|chúc bạn|have a (good|great|nice|wonderful) day|"
    r"good morning|good afternoon|good evening|tôi thấy rất vui|hôm nay tôi"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# 2. TABLE_LOOKUP regex: explicit tabular/spreadsheet signals
TABLE_LOOKUP_PATTERNS = re.compile(
    r"\b("
    r"bảng lương|bậc lương|dải lương|thang lương|bảng phân bổ|ma trận lương|ma trận|"
    r"sheet|spreadsheet|worksheet|file excel|tập tin excel|"
    r"doanh thu q[1-4]|ngân sách q[1-4]|revenue q[1-4]|budget q[1-4]|"
    r"cột|dòng|hàng \d+|bảng số liệu|bảng tổng hợp số liệu|bảng tổng hợp|"
    r"salary table|salary matrix|pay scale|salary bracket|"
    r"((budget|revenue|expense|summary|allocation|training|data) table)|"
    r"(table (of|for) .+ (budget|revenue|expense|salary|allocation))"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# 3. MULTI_DOC_COMPARISON regex: explicit comparative signals across entities/docs
MULTI_DOC_PATTERNS = re.compile(
    r"\b("
    r"so sánh .+ (và|với) .+|"
    r"đối chiếu .+ (và|với) .+|"
    r"điểm khác biệt giữa .+ (và|với) .+|"
    r"khác nhau như thế nào giữa .+ (và|với) .+|"
    r"khác gì so với .+|"
    r"compare .+ (and|with|versus|vs) .+|"
    r"(differences|difference|differ) between .+ (and|with) .+|"
    r"differ between .+ and .+|"
    r"versus| vs "
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# 4. OUT_OF_DOMAIN regex: open-domain world knowledge, weather, celebrities, sports, geography
OUT_OF_DOMAIN_PATTERNS = re.compile(
    r"\b("
    r"thời tiết|dự báo thời tiết|weather|forecast|"
    r"thủ đô của|capital (city )?of|"
    r"tổng thống|thủ tướng|chủ tịch nước của (nước|quốc gia)|president of|prime minister of|"
    r"vô địch cúp c1|vô địch world cup|fifa world cup|world cup|uefa|champions league|super bowl|ngoại hạng anh|premier league|"
    r"giá vàng|tỷ giá usd|chứng khoán dow jones|giá bitcoin|stock price|nasdaq|dow jones|s&p 500|"
    r"ai là đạo diễn|phim chiếu rạp|diễn viên|ca sĩ|bài hát hot|oscar|academy award|grammy|"
    r"khoảng cách giữa|distance between|trái đất và mặt trăng|earth and the moon|mặt trời|solar system"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


@dataclass(frozen=True)
class HeuristicMatchResult:
    """Result of Tier-1 fast heuristic evaluation."""

    matched: bool
    intent: IntentLabel | None = None
    confidence: float = 0.0
    rule_name: str | None = None
    target_departments: list[str] = field(default_factory=list)
    reasoning: str | None = None


class FastPathHeuristics:
    """High-speed token and regex pattern matching achieving sub-5ms routing."""

    def __init__(self, confidence_threshold: float = 0.98) -> None:
        self.confidence_threshold = confidence_threshold

    def extract_departments(self, query: str) -> list[str]:
        """Detect referenced organizational departments in the query text."""
        matched_depts = []
        for dept_name, pattern in DEPARTMENT_PATTERNS.items():
            if pattern.search(query):
                matched_depts.append(dept_name)
        return matched_depts

    def evaluate(self, query: str) -> HeuristicMatchResult:
        """Run fast-path rules in priority order."""
        cleaned = query.strip()
        depts = self.extract_departments(cleaned)

        # 1. Fast match for GENERAL_CHAT (greetings, pleasantries, small talk)
        # Avoid false positives if query also mentions policies or departments
        if GENERAL_CHAT_EXACT.search(cleaned) and not (
            "chính sách" in cleaned.lower()
            or "quy định" in cleaned.lower()
            or "quy chế" in cleaned.lower()
            or len(depts) > 0
        ):
            # If query is short or pure pleasantry
            if len(cleaned.split()) <= 10:
                return HeuristicMatchResult(
                    matched=True,
                    intent=IntentLabel.GENERAL_CHAT,
                    confidence=0.99,
                    rule_name="FAST_GENERAL_CHAT",
                    target_departments=depts,
                    reasoning="Matched pure conversational greeting or polite chat pattern",
                )

        # 2. Fast match for OUT_OF_DOMAIN (weather, world geography, external sports)
        if OUT_OF_DOMAIN_PATTERNS.search(cleaned):
            return HeuristicMatchResult(
                matched=True,
                intent=IntentLabel.OUT_OF_DOMAIN,
                confidence=0.99,
                rule_name="FAST_OUT_OF_DOMAIN",
                target_departments=depts,
                reasoning="Matched external open-domain query pattern (weather, trivia, sports)",
            )

        # 3. Fast match for MULTI_DOC_COMPARISON (explicit comparison between departments or policies)
        if MULTI_DOC_PATTERNS.search(cleaned) or (len(depts) >= 2 and ("so sánh" in cleaned.lower() or "khác" in cleaned.lower() or "compare" in cleaned.lower())):
            return HeuristicMatchResult(
                matched=True,
                intent=IntentLabel.MULTI_DOC_COMPARISON,
                confidence=0.98,
                rule_name="FAST_MULTI_DOC_COMPARISON",
                target_departments=depts,
                reasoning="Matched multi-entity comparative pattern across departments or policies",
            )

        # 4. Fast match for TABLE_LOOKUP (matrices, salary tables, spreadsheets)
        if TABLE_LOOKUP_PATTERNS.search(cleaned):
            return HeuristicMatchResult(
                matched=True,
                intent=IntentLabel.TABLE_LOOKUP,
                confidence=0.98,
                rule_name="FAST_TABLE_LOOKUP",
                target_departments=depts,
                reasoning="Matched explicit tabular coordinate or salary matrix keyword",
            )

        return HeuristicMatchResult(matched=False)
