import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

# Harness tests do not require the SDK, a model server, or network access.
with patch.dict(sys.modules, {'openai': types.SimpleNamespace(OpenAI=Mock())}):
    spec = importlib.util.spec_from_file_location('agent', Path(__file__).parents[1] / 'agent.py')
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)


def message(content='', arguments=None, name='run_bash', reasoning=''):
    calls = None if arguments is None else [types.SimpleNamespace(
        id='call-1', function=types.SimpleNamespace(name=name, arguments=arguments))]
    return types.SimpleNamespace(content=content, tool_calls=calls,
                                 model_extra={'reasoning_content': reasoning})


class AgentTests(unittest.TestCase):
    def run_loop(self, responses, gate=(True, '')):
        with patch.object(agent, 'MAX_ITERATIONS', len(responses)), \
             patch.object(agent, 'call_llm_resilient', side_effect=[(m, False) for m in responses]), \
             patch.object(agent, 'execute_bash', return_value=(0, 'ok')) as execute, \
             patch.object(agent, 'check_verification_gate', return_value=gate), \
             contextlib.redirect_stdout(io.StringIO()):
            outcome = agent.run_agent_loop(None, 'fake', 'Investigate')
        return outcome, execute

    def test_prose_fences_and_reasoning_are_not_actions(self):
        outcome, execute = self.run_loop([
            message('I will investigate.'), message('```bash\ntouch bad\n```'),
            message(reasoning='<bash>touch bad</bash>')])
        self.assertEqual(outcome, agent.Outcome.EXHAUSTED)
        execute.assert_not_called()

    def test_invalid_arguments_allow_recovery(self):
        for arguments in ('[]', 'null', '{', '{"command": 3}', '{"command":""}',
                          '{"command":"true","extra":true}'):
            with self.subTest(arguments=arguments):
                outcome, execute = self.run_loop([
                    message(arguments=arguments), message('<done>Reviewed</done>')])
                self.assertEqual(outcome, agent.Outcome.COMPLETED)
                execute.assert_not_called()

    def test_native_command_and_completion(self):
        outcome, execute = self.run_loop([
            message(arguments=json.dumps({'command': 'true'})),
            message(arguments=json.dumps({'summary': 'Reviewed'}), name='finish_task')])
        self.assertEqual(outcome, agent.Outcome.COMPLETED)
        execute.assert_called_once_with('true')

    def test_failed_verification_exhausts(self):
        outcome, _ = self.run_loop([message('<done>Reviewed</done>')], (False, 'Tests failed'))
        self.assertEqual(outcome, agent.Outcome.EXHAUSTED)

    def test_ambiguous_actions_rejected(self):
        _, execute = self.run_loop([message('<bash>true</bash><bash>false</bash>')])
        execute.assert_not_called()

    def test_text_action_whitespace_and_case_are_tolerated(self):
        outcome, execute = self.run_loop([message('<BASH>\n  true \n</BASH>'),
                                          message('<Done> Reviewed </Done>')])
        self.assertEqual(outcome, agent.Outcome.COMPLETED)
        execute.assert_called_once_with('true')

    def test_unterminated_whitespace_action_is_rejected_quickly(self):
        # Degenerate model output must not stall the controller in the parser.
        started = time.monotonic()
        outcome, execute = self.run_loop([message('<bash>' + ' ' * 20000 + 'x'),
                                          message('<done>' + '\n' * 20000 + 'x')])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(outcome, agent.Outcome.EXHAUSTED)
        execute.assert_not_called()

    def test_api_failure_is_failed_outcome(self):
        outcome, _ = self.run_loop([None])
        self.assertEqual(outcome, agent.Outcome.FAILED)

    def test_single_task_exit_status(self):
        with patch.object(agent, 'OpenAI'), patch.object(agent, 'get_active_model', return_value='fake'), \
             patch.object(agent, 'CONTINUOUS', False), patch.object(sys, 'argv', ['agent.py', 'task']), \
             patch.object(agent, 'run_agent_loop', return_value=agent.Outcome.EXHAUSTED), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.main(), 2)

    def test_infrastructure_errors_do_not_trigger_parser_fallback(self):
        client = Mock()
        client.chat.completions.create.side_effect = RuntimeError('connection refused')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(agent.call_llm_resilient(client, 'fake', []), (None, False))
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_parser_failure_uses_text_fallback(self):
        client = Mock()
        reply = message('<done>Reviewed</done>')
        client.chat.completions.create.side_effect = [RuntimeError('peg-native parser'),
            types.SimpleNamespace(choices=[types.SimpleNamespace(message=reply)])]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.call_llm_resilient(client, 'fake', []), (reply, True))
        self.assertNotIn('tools', client.chat.completions.create.call_args.kwargs)

    def test_output_flood_is_drained_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'WORKSPACE', directory), \
             contextlib.redirect_stdout(io.StringIO()):
            code, output = agent.execute_bash("python3 -c 'import os; os.write(1, b\"x\" * 2000000)' ")
        self.assertEqual(code, 0)
        self.assertLess(len(output), agent.MAX_OUTPUT_CHARS + 100)
        self.assertIn('truncated', output)

    def test_timeout_cleans_up_child(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'survivor'
            child = f'import time; from pathlib import Path; time.sleep(.5); Path({str(marker)!r}).touch()'
            with patch.object(agent, 'WORKSPACE', directory), patch.object(agent, 'COMMAND_TIMEOUT', .1), \
                 contextlib.redirect_stdout(io.StringIO()):
                code, _ = agent.execute_bash(f'python3 -c {shlex.quote(child)} & wait')
            time.sleep(.7)
            self.assertEqual(code, 124)
            self.assertFalse(marker.exists())

    def test_model_must_be_configured_explicitly(self):
        with patch.object(agent, 'MODEL_ENV', ''), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                agent.get_active_model()
        self.assertEqual(raised.exception.code, 1)
        with patch.object(agent, 'MODEL_ENV', 'chosen/model'):
            self.assertEqual(agent.get_active_model(), 'chosen/model')

    def test_history_is_trimmed_to_token_budget_keeping_tool_exchanges_whole(self):
        call = {'id': 'c', 'function': {'name': 'run_bash', 'arguments': '{"command":"ls"}'}}
        history = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'task'}]
        for index in range(10):
            history += [{'role': 'assistant', 'content': '', 'tool_calls': [call]},
                        {'role': 'tool', 'tool_call_id': 'c', 'content': f'{index}' + 'x' * 3000}]
        with patch.object(agent, 'MAX_HISTORY_MESSAGES', 100), patch.object(agent, 'MAX_CONTEXT_TOKENS', 3000):
            trimmed = agent.trim_history(history)
        self.assertEqual(trimmed[:2], history[:2])
        self.assertLess(len(trimmed), len(history))
        self.assertEqual(trimmed[-1], history[-1])
        self.assertNotEqual(trimmed[2]['role'], 'tool')
        self.assertLessEqual(sum(map(agent.estimate_tokens, trimmed)), 3000 + agent.estimate_tokens(history[-1]))

    def test_newest_message_survives_even_if_oversized(self):
        history = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 't'},
                   {'role': 'user', 'content': 'y' * 100000}]
        with patch.object(agent, 'MAX_CONTEXT_TOKENS', 100):
            self.assertEqual(agent.trim_history(history), history)

    def test_zero_limits_disable_trimming(self):
        history = [{'role': 'user', 'content': 'x' * 100} for _ in range(50)]
        with patch.object(agent, 'MAX_HISTORY_MESSAGES', 0), patch.object(agent, 'MAX_CONTEXT_TOKENS', 0):
            self.assertEqual(agent.trim_history(history), history)


if __name__ == '__main__':
    unittest.main()
