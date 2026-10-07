"""正文草稿与核验结果的消费者边界，不依赖模型服务或数据库。"""
import unittest
from langchain_core.messages import AIMessageChunk
from langgraph.graph import StateGraph, START, END
from app.agent.nodes.answer import make_generate
from app.agent.prompt import _KNOWLEDGE_REFUSAL
from app.agent.state import SupportState


class StreamModel:
    def __init__(self, chunks):
        self.chunks = chunks

    async def astream(self, messages, config=None):
        for chunk in self.chunks:
            yield chunk


class AnswerStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, chunks, item):
        graph = StateGraph(SupportState)
        graph.add_node('generate', make_generate(StreamModel(chunks)))
        graph.add_edge(START, 'generate')
        graph.add_edge('generate', END)
        events = [event async for event in graph.compile().astream_events(
            {'request_results': [item]}, version='v2')]
        deltas = [event['data']['text'] for event in events
                  if event['event'] == 'on_custom_event'
                  and event['name'] == 'support_answer_delta']
        result = next(event['data']['output'] for event in events
                      if event['event'] == 'on_chain_end' and event['name'] == 'generate')
        return deltas, result

    async def test_invalid_citation_draft_is_not_authoritative_answer(self):
        deltas, result = await self.collect(
            [AIMessageChunk(content='草稿事实'), AIMessageChunk(content=' [99]')],
            {'intent': 'product_qa', 'goal': 'faq', 'question': '问题', 'retrieval': {},
             'tools': [], 'citations': [{'number': 1, 'chunkId': 'source'}]},
        )
        self.assertEqual(deltas, ['草稿事实', ' [99]'])
        self.assertEqual(result['answer'], _KNOWLEDGE_REFUSAL)
        self.assertEqual(result['citations'], [])
        self.assertEqual(result['low_confidence'][0]['entrypoint'], 'generation_missing_citation')

    async def test_only_display_text_blocks_reach_draft_and_answer(self):
        deltas, result = await self.collect([
            AIMessageChunk(content=[
                {'type': 'reasoning', 'text': 'PRIVATE_REASONING'},
                {'type': 'tool_use', 'text': 'PRIVATE_INTERNAL_JSON'},
                {'type': 'text', 'text': '可见正文'},
                {'type': 'output_text', 'text': '第二段'},
            ], tool_call_chunks=[{'name': 'PRIVATE_TOOL', 'args': '{"secret":1}',
                                  'id': 'call', 'index': 0}]),
        ], {'intent': 'other', 'goal': 'smalltalk', 'question': '你好', 'retrieval': {}})
        self.assertEqual(deltas, ['可见正文第二段'])
        self.assertEqual(result['answer'], '可见正文第二段')


if __name__ == '__main__':
    unittest.main()
