"""Request-view regressions: no historical, failed, or numeric reads as prose."""
import json

import pytest

from library.agent.conversation_compaction import TokenCounter
from library.agent.live_turn_context import fit_live_turn
from library.llm import ChatMessage, ToolResultBlock, ToolUseBlock


def exchange(key, name='read_files', *, failed=False, pixel=False):
    text = key + ' specific evidence ' + ('x' * 10_000)
    read = {'ok': not failed, 'text': text, 'extras': {'page_start': 3, 'page_end': 3}}
    if pixel:
        read['args'] = {'numeric_check': {}}
    packet = {'ok': True, 'results': [{'entry_id': key, 'reads': [read]}]}
    return [ChatMessage(role='assistant', content=[ToolUseBlock(key, name, {})]),
            ChatMessage(role='tool', content=[ToolResultBlock(key, json.dumps(packet))])]


def tool_ids(messages):
    return [b.tool_call_id for m in messages if m.role == 'tool' for b in m.content]


def test_only_current_successful_prose_exchange_is_replayed_after_two_compactions():
    counter = TokenCounter('utf8_upper_bound')
    question = ChatMessage(role='user', content='Which specific evidence is relevant?')
    tail = ChatMessage(role='user', content='Finish the answer.')
    tape = [*exchange('historical'), question, *exchange('other', 'search_metadata'),
            *exchange('failed', failed=True), *exchange('pixels', pixel=True), *exchange('valid'), tail]
    for _ in range(2):
        fitted, _, replayed = fit_live_turn(tape, question, token_budget=8_000, counter=counter)
        assert replayed == 1 and tool_ids(fitted) == ['valid']
        assert any(m is question for m in fitted) and fitted[-1] is tail
        assert counter.messages(fitted) <= 8_000
        message = next(m for m in fitted if m.role == 'tool')
        packet = json.loads(message.content[0].content)
        assert packet['view'] == 'read_excerpt' and 'partial' in packet['coverage']
        excerpt = packet['excerpts'][0]
        assert excerpt['entry_id'] == 'valid' and excerpt['wire_offset'] == 0
        assert excerpt['text'].startswith('valid specific evidence')
        tape = [*fitted, ChatMessage(role='assistant', content='unrelated' * 2500), tail]


def test_question_that_cannot_fit_fails_instead_of_summarizing_it():
    question = ChatMessage(role='user', content='中文原题' * 300)
    with pytest.raises(ValueError, match='current user request'):
        fit_live_turn([question], question, token_budget=900, counter=TokenCounter('utf8_upper_bound'))
