"""沿用一个 Langfuse 回调，仅补充受控根完成标志与封闭意图上下文。"""
import logging
from typing import get_args

from langfuse.langchain import CallbackHandler
from app.agent.intent import SupportRequest

from app.services.observability.console import INTENTS, number

logger = logging.getLogger(__name__)


class SupportCallbackHandler(CallbackHandler):
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       metadata=None, **kwargs):
        """仅首次根运行写入受控关联；节点不重复绑定回调。"""
        result = super().on_chain_start(serialized, inputs, run_id=run_id,
                                        parent_run_id=parent_run_id, metadata=metadata, **kwargs)
        if parent_run_id is None:
            try:
                root = self.runs.get(run_id)
                if root is not None:
                    controlled = {key: value for key, value in (metadata or {}).items()
                                  if key in {'conversation_id', 'assistant_message_id', 'aiden_source'}}
                    root.update(metadata=controlled)
                    root.update_trace(metadata=controlled)
            except Exception:
                logger.warning('受控根关联写入失败')
        return result

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        """成功事件显式标记图完成；业务答复质量不在此状态内。"""
        if parent_run_id is None:
            try:
                root = self.runs.get(run_id)
                if root is not None:
                    requests = outputs.get('requests', []) if isinstance(outputs, dict) else []
                    goals = get_args(SupportRequest.model_fields['goal'].annotation)
                    recognized = [
                        {'intent': item['intent'], **({'goal': item['goal']} if item.get('goal') in goals else {})}
                        for item in requests if isinstance(item, dict) and item.get('intent') in INTENTS
                    ]
                    controlled = {'aiden_completion': 'success'}
                    if isinstance(outputs, dict) and isinstance(outputs.get('requests'), list):
                        controlled['recognized_intents'] = recognized
                    root.update(metadata=controlled)
                    root.update_trace(metadata=controlled)
            except Exception:
                # 观测失败不得阻断在线图；不打印上游内容或身份。
                logger.warning('受控根观测标志写入失败')
        return super().on_chain_end(outputs, run_id=run_id, parent_run_id=parent_run_id, **kwargs)

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        """仅保留响应实际携带的 provider 计数；缺字段不推算。"""
        try:
            observation = self.runs.get(run_id)
            response_generation = response.generations[-1][-1]
            message = getattr(response_generation, 'message', None)
            usage = getattr(message, 'usage_metadata', None)
            raw = usage if isinstance(usage, dict) else {}
            output = response.llm_output if isinstance(response.llm_output, dict) else {}
            fallback = output.get('token_usage')
            if not raw and isinstance(fallback, dict):
                raw = {'input_tokens': fallback.get('prompt_tokens'),
                       'output_tokens': fallback.get('completion_tokens'),
                       'total_tokens': fallback.get('total_tokens')}
            controlled = {key: raw[field] for key, field in
                          [('input', 'input_tokens'), ('output', 'output_tokens'), ('total', 'total_tokens')]
                          if number(raw.get(field)) is not None}
            if observation is not None:
                observation.update(metadata={'aiden_usage_source': 'provider' if controlled else 'unknown',
                                             'aiden_provider_usage': controlled})
        except Exception:
            logger.warning('受控 provider usage 标记写入失败')
        return super().on_llm_end(response, run_id=run_id, parent_run_id=parent_run_id, **kwargs)
