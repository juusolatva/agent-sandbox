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


if __name__ == '__main__':
    unittest.main()
