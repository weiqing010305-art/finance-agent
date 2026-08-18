from __future__ import annotations

from dataclasses import dataclass
import re

from backend.schemas import Intent, RouteDecision


@dataclass(frozen=True)
class RoutingContext:
    case_id: str | None = None
    company: str | None = None
    has_pending_confirmation: bool = False
    active_run_status: str | None = None
    has_report: bool = False
    report_has_evidence: bool = False


_AFFIRMATIVE = re.compile(r"^(是|是的|对|对的|确认|可以|好的|好|继续)[。！! ]*$")
_NEGATIVE = re.compile(r"^(不|不是|不对|否|取消|不要)[。！! ]*$")
_PAUSE = re.compile(r"^(请)?(暂停|先停一下|停一下|停止研究)[。！! ]*$")
_RESUME = re.compile(r"^(请)?(继续|恢复|继续执行|继续研究)[。！! ]*$")
_STATUS = re.compile(r"^(查(询)?|看(看)?)(一下)?(任务)?(状态|进度)[？?。 ]*$")
_RESEARCH = re.compile(
    r"(^|[，,。；;]\s*)(?:(谢谢|感谢)[，,\s]*)?"
    r"(?:(请|帮我|麻烦|我想|想要|需要|给我)\s*)?"
    r"(?:(再|继续|补充|重新)\s*)?"
    r"(研究|调研|分析|查一下|查询一下|看看|看一下)"
)
_SOCIAL = re.compile(r"^(好的|好|谢谢|感谢|辛苦了|明白了|收到)([，, ]*(谢谢|感谢))?[。！! ]*$")
_OUT_OF_SCOPE = re.compile(r"(天气|彩票|星座|写代码|这段代码|代码实现|程序代码|编程|翻译|菜谱|体育比分)")
_FINANCE_SCOPE = re.compile(r"(公司|企业|行业|股票|证券|财务|盈利|估值|市场|投资)")
_INJECTION = re.compile(
    r"("
    # 中文：忽略/无视/忘掉/跳过 + 指令/规则/之前/上面/系统提示
    r"(?:忽略|无视|不管|不要管|别管|跳过|忘掉|忘记|停止|禁止|不用理|不必理会|别听)"
    r".{0,30}(?:指令|规则|限制|权限|调用|内容|消息|对话|上下文|提示|要求|预设|系统|"
    r"之前|上面|前面|protocol|instructions|prompt|rules)|"
    # 中文：不要/请勿 + 遵循/遵守 + 指令/规则/系统
    r"(?:不要|别|请勿|禁止|无需|不必).{0,20}(?:遵循|遵守|听从|执行|理会)"
    r".{0,20}(?:指令|规则|提示|系统|之前|上面|前面)|"
    # 中文：打印/泄露/展示系统提示或密钥
    r"(?:打印|重复|复述|泄露|展示|输出|告诉我|背诵).{0,20}"
    r"(?:系统提示|system prompt|指令|规则|密钥|secret|api key|password)|"
    # 中文：角色冒充/越狱
    r"(?:你现在是|假装你是|扮演|越狱|脱轨|绕过审查|不受限)"
    r"|"
    # 英文：忽略/绕过之前指令
    r"(?:ignore|disregard|bypass|skip|overlook|forget).{0,40}"
    r"(?:previous|prior|above|earlier|system|all|any|the|these|those).{0,20}"
    r"(?:instruction|prompt|rule|message|context|guideline|policy|restriction)|"
    # 英文：重复/泄露提示词
    r"(?:repeat|print|reveal|show|display|output).{0,20}"
    r"(?:system prompt|instructions|secret|api key|password)|"
    # 英文：角色冒充/越狱关键词
    r"(?:you are now|pretend|jailbreak|do anything now|unrestricted)"
    r")",
    re.IGNORECASE,
)
_QUESTION = re.compile(r"(呢|吗|如何|怎么样|为什么|多少|是否|有没有|？|\?)")
_NEGATED_RESEARCH = re.compile(r"(不用|不要|别|无需|不需要|停止|取消).{0,8}(再)?(研究|调研|分析|查|看)")
_GRATITUDE = re.compile(r"(谢谢|感谢|辛苦)")
_META_RESEARCH = re.compile(
    r"((研究|调研|分析)(工具|流程|过程|能力)|"
    r"(怎么|如何).{0,12}(做|进行)?(研究|调研|分析)|"
    r"(支持哪些|有什么|介绍).{0,12}(研究|调研|分析).{0,6}(类型|能力|功能)?)"
)
_CAPABILITY_META = re.compile(
    r"(你能做什么|支持哪些|能介绍.{0,12}(能力|功能)|"
    r"(研究|调研|分析|查询|查询一下|看看|看一下).{0,10}"
    r"(功能|能力|怎么用|如何用|怎么使用|如何使用|使用方法|收费|区别|差别|"
    r"帮助文档|页面在哪里|入口在哪里|是什么意思|什么含义)|"
    r"(看看|看一下).{0,8}(怎么用|怎么使用|如何使用|你能做什么))"
)
_RESEARCH_HOMOGRAPH = re.compile(r"^(研究生|研究人员|分析师|分析员)")
_REPORT_REFERENCE = re.compile(
    r"(这份分析|分析结果|分析得|分析是否|分析有没有|刚才.{0,8}分析|报告|结论|"
    r"你的分析|这个分析|依据|来源|准确|可靠|遗漏)"
)
_REPORT_EVALUATION = re.compile(
    r"分析.{0,8}(完整|合理|全面|准确|可靠|可信|遗漏|靠谱吗|有道理)"
)
_NON_TARGET_AFTER_VERB = re.compile(
    r"^(一下)?(是什么意思|什么含义|怎么用|如何用|怎么使用|如何使用|使用方法|"
    r"帮助文档|页面在哪里|入口在哪里|完整吗|合理吗|全面吗|靠谱吗|收费)"
)
_FINANCIAL_TARGET = re.compile(
    r"(公司|企业|行业|股票|证券|财务|财报|盈利|利润|收入|营收|毛利|现金流|"
    r"负债|资产|估值|股价|市场|投资|分红|风险|业绩|成本|费用|增长|供应链|"
    r"管理层|治理|商业模式|竞争格局|银行|保险|基金|债券|业务)"
)
_COMPANY_ENTITY = re.compile(
    r"(腾讯(?:控股)?|阿里巴巴|阿里|比亚迪|贵州茅台|茅台|宁德时代|小米(?:集团)?|"
    r"美团|百度|京东|拼多多|华为|苹果|微软|谷歌|特斯拉|英伟达|"
    r"[\u4e00-\u9fffA-Za-z0-9]{2,20}(?:公司|集团|股份|银行|证券|保险)|"
    r"\b(?:\d{6}\.(?:SH|SZ)|\d{4,5}\.HK|[A-Z]{1,5}\.(?:US|NASDAQ|NYSE))\b)"
)
_CASE_REFERENCE = re.compile(r"(它|其|该公司|这家公司|当前公司|这只股票|该股|这家企业)")
_PRODUCT_UI = re.compile(
    r"(使用说明|帮助文档|设置|按钮|页面在哪|页面在哪里|入口在哪|入口在哪里|"
    r"怎么使用|如何使用|怎么用|如何用)"
)


def _has_concrete_research_target(
    text: str, match: re.Match[str], context: RoutingContext
) -> bool:
    target = text[match.end():].strip(" ，,。；;！？?!")
    if not target or _NON_TARGET_AFTER_VERB.search(target):
        return False
    target = target.removeprefix("一下").strip()
    if not target or _PRODUCT_UI.search(target):
        return False
    if _FINANCIAL_TARGET.search(target) or _COMPANY_ENTITY.search(target):
        return True
    if context.company and context.company in target:
        return True
    return bool(context.case_id and _CASE_REFERENCE.search(target))


def _decision(
    intent: Intent,
    context: RoutingContext,
    *,
    confidence: float,
    response_policy: str,
    reason_codes: list[str],
) -> RouteDecision:
    research = intent in {"RESEARCH_NEW", "RESEARCH_FOLLOWUP"}
    return RouteDecision(
        intent=intent,
        confidence=confidence,
        case_id=context.case_id,
        requires_planner=research,
        external_research_allowed=False,
        response_policy=response_policy,
        reason_codes=reason_codes,
    )


def route_by_rules(message: str, context: RoutingContext | None = None) -> RouteDecision:
    context = context or RoutingContext()
    text = re.sub(r"\s+", " ", message).strip()
    if not text:
        return _decision(
            "CLARIFICATION", context, confidence=1.0,
            response_policy="ask_clarification", reason_codes=["EMPTY_MESSAGE"],
        )

    if context.has_pending_confirmation:
        if _AFFIRMATIVE.fullmatch(text):
            return _decision(
                "CONFIRMATION", context, confidence=0.99,
                response_policy="await_confirmation_handler",
                reason_codes=["PENDING_CONFIRMATION", "AFFIRMATIVE_REPLY"],
            )
        if _NEGATIVE.fullmatch(text):
            return _decision(
                "CONFIRMATION", context, confidence=0.99,
                response_policy="await_confirmation_handler",
                reason_codes=["PENDING_CONFIRMATION", "NEGATIVE_REPLY"],
            )

    if _PAUSE.fullmatch(text):
        return _decision(
            "CONTROL", context, confidence=0.99,
            response_policy="handle_control", reason_codes=["PAUSE_COMMAND"],
        )
    if _RESUME.fullmatch(text):
        return _decision(
            "CONTROL", context, confidence=0.99,
            response_policy="handle_control", reason_codes=["RESUME_COMMAND"],
        )
    if _STATUS.fullmatch(text):
        return _decision(
            "CONTROL", context, confidence=0.98,
            response_policy="handle_control", reason_codes=["STATUS_COMMAND"],
        )

    if _INJECTION.search(text):
        return _decision(
            "AMBIGUOUS", context, confidence=0.95,
            response_policy="ask_clarification", reason_codes=["UNTRUSTED_CONTROL_LANGUAGE"],
        )

    if _RESEARCH_HOMOGRAPH.search(text) or _CAPABILITY_META.search(text):
        return _decision(
            "AMBIGUOUS", context, confidence=0.93,
            response_policy="ask_clarification", reason_codes=["META_OR_HOMOGRAPH_QUERY"],
        )

    if _OUT_OF_SCOPE.search(text) and not _FINANCE_SCOPE.search(text):
        return _decision(
            "OUT_OF_SCOPE", context, confidence=0.96,
            response_policy="reject_out_of_scope", reason_codes=["NON_FINANCE_TOPIC"],
        )

    if _NEGATED_RESEARCH.search(text):
        if _GRATITUDE.search(text):
            return _decision(
                "SOCIAL_ACK", context, confidence=0.98,
                response_policy="template_reply",
                reason_codes=["NEGATED_RESEARCH", "SOCIAL_PHRASE"],
            )
        return _decision(
            "CONTROL", context, confidence=0.96,
            response_policy="handle_control", reason_codes=["STOP_RESEARCH_COMMAND"],
        )

    if _GRATITUDE.search(text) and not _RESEARCH.search(text):
        return _decision(
            "SOCIAL_ACK", context, confidence=0.97,
            response_policy="template_reply", reason_codes=["SOCIAL_PHRASE"],
        )

    if (
        context.has_report
        and (_REPORT_REFERENCE.search(text) or _REPORT_EVALUATION.search(text))
        and _QUESTION.search(text)
    ):
        return _decision(
            "REPORT_QA", context, confidence=0.94,
            response_policy="answer_from_existing_evidence",
            reason_codes=["EXISTING_REPORT", "REPORT_REFERENCE"],
        )

    if _META_RESEARCH.search(text):
        return _decision(
            "AMBIGUOUS", context, confidence=0.9,
            response_policy="ask_clarification", reason_codes=["META_RESEARCH_QUESTION"],
        )

    research_match = _RESEARCH.search(text)
    if research_match and _has_concrete_research_target(text, research_match, context):
        if context.case_id:
            return _decision(
                "RESEARCH_FOLLOWUP", context, confidence=0.96,
                response_policy="enqueue_at_safe_checkpoint",
                reason_codes=["ACTIVE_CASE", "EXPLICIT_ANALYSIS_VERB"],
            )
        return _decision(
            "RESEARCH_NEW", context, confidence=0.97,
            response_policy="await_entity_resolution",
            reason_codes=["EXPLICIT_ANALYSIS_VERB"],
        )

    if research_match:
        return _decision(
            "AMBIGUOUS", context, confidence=0.9,
            response_policy="ask_clarification", reason_codes=["RESEARCH_TARGET_MISSING"],
        )

    if context.has_report and _QUESTION.search(text):
        if context.report_has_evidence:
            return _decision(
                "REPORT_QA", context, confidence=0.9,
                response_policy="answer_from_existing_evidence",
                reason_codes=["EXISTING_REPORT", "ELLIPTICAL_QUESTION"],
            )
        return _decision(
            "RESEARCH_FOLLOWUP", context, confidence=0.82,
            response_policy="enqueue_at_safe_checkpoint",
            reason_codes=["EXISTING_REPORT", "EVIDENCE_INSUFFICIENT"],
        )

    if _SOCIAL.fullmatch(text):
        return _decision(
            "SOCIAL_ACK", context, confidence=0.99,
            response_policy="template_reply", reason_codes=["SOCIAL_PHRASE"],
        )

    return _decision(
        "AMBIGUOUS", context, confidence=0.5,
        response_policy="ask_clarification", reason_codes=["NO_DETERMINISTIC_MATCH"],
    )
