"""客服 Agent 的静态工具注册入口。

新增工具需先实现业务契约，再在此登记；意图白名单仍由 Agent 图独立决定。
运行时不会根据模型输出或外部服务动态发现工具。
"""

from langchain_core.tools import BaseTool

from app.services.tools.after_sale import query_after_sale
from app.services.tools.after_sale_submit import query_refund_policy, submit_after_sale
from app.services.tools.customer import query_logistics, query_order, search_faq
from app.services.tools.ticket_create import create_ticket
from app.services.tools.ticket_query import query_ticket


SUPPORT_TOOLS: tuple[BaseTool, ...] = (
    query_order,
    query_logistics,
    search_faq,
    query_after_sale,
    query_refund_policy,
    submit_after_sale,
    create_ticket,
    query_ticket,
)
SUPPORT_TOOLS_BY_NAME: dict[str, BaseTool] = {tool.name: tool for tool in SUPPORT_TOOLS}

if len(SUPPORT_TOOLS_BY_NAME) != len(SUPPORT_TOOLS):
    raise ValueError("客服工具名重复")
