#!/usr/bin/env python3
"""Synthetic-only privacy, modern-rollout and MCP tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import context_core as core
import mcp_server
import report

SID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
SECRET = 'SYNTHETIC_PRIVATE_9327'
ATTACK = '<img src=x onerror="alert(1)">'


def line(kind, payload, second=0):
    return {'timestamp': f'2026-11-14T02:00:{second:02d}.000Z', 'type': kind, 'payload': payload}


def fixture(home: Path) -> Path:
    day = home / 'sessions' / '2026' / '11' / '14'
    day.mkdir(parents=True, exist_ok=True)
    path = day / f'rollout-2026-11-14T02-00-00-{SID}.jsonl'
    def tokens(total, last):
        return line('event_msg', {'type': 'token_count', 'info': {
            'total_token_usage': {'input_tokens': total, 'cached_input_tokens': 100,
                'output_tokens': 50, 'reasoning_output_tokens': 10, 'total_tokens': total + 50},
            'last_token_usage': {'input_tokens': last, 'cached_input_tokens': 50,
                'output_tokens': 20, 'reasoning_output_tokens': 5, 'total_tokens': last + 20},
            'model_context_window': 10000}}, 10)
    rows = [
        line('session_meta', {'id': SID, 'cwd': '/synthetic/' + SECRET,
             'parent_thread_id': '11111111-1111-4111-8111-111111111111'}),
        line('turn_context', {'model': 'gpt-5-codex'}, 1),
        line('event_msg', {'type': 'task_started', 'started_at': 1000}, 2),
        line('response_item', {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': SECRET + ATTACK}]}, 3),
        line('response_item', {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': '合成助手回答'}]}, 4),
        line('response_item', {'type': 'function_call', 'name': 'exec_command', 'call_id': 'call-a',
             'arguments': json.dumps({'cmd': 'echo ' + SECRET})}, 5),
        line('response_item', {'type': 'function_call_output', 'call_id': 'call-a', 'output': SECRET}, 6),
        tokens(1000, 400), tokens(2000, 500),
        line('event_msg', {'type': 'item_completed', 'started_at_ms': 1000000, 'completed_at_ms': 1015000,
             'item': {'type': 'CommandExecution', 'id': 'cmd-1', 'command': ['echo', SECRET],
                      'cwd': '/synthetic', 'status': 'failed', 'exit_code': 2,
                      'aggregated_output': SECRET, 'parsed_cmd': [
                          {'type': 'read', 'path': '/synthetic/' + SECRET},
                          {'type': 'search', 'path': '/synthetic/search'}]}}, 15),
        line('event_msg', {'type': 'item_completed', 'completed_at_ms': 1016000,
             'item': {'type': 'FileChange', 'id': 'file-1', 'changes': {'/synthetic/changed.txt': {'type': 'update'}},
                      'status': 'completed'}}, 16),
        line('event_msg', {'type': 'item_completed', 'completed_at_ms': 1017000,
             'item': {'type': 'CollabAgentToolCall', 'id': 'agent-1', 'tool': 'spawn_agent',
                      'sender_thread_id': SID, 'receiver_thread_ids': ['22222222-2222-4222-8222-222222222222'],
                      'status': 'completed', 'prompt': SECRET}}, 17),
        line('inter_agent_communication', {'author': '/root', 'recipient': '/root/worker',
             'other_recipients': [], 'content': SECRET, 'trigger_turn': True}, 18),
        line('compacted', {'message': SECRET}, 19),
        line('event_msg', {'type': 'context_compacted'}, 20),
        line('event_msg', {'type': 'task_complete', 'started_at': 1000, 'completed_at': 1060,
             'duration_ms': 60000, 'time_to_first_token_ms': 230}, 21),
    ]
    with path.open('w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    return path


class TestDetailed(unittest.TestCase):
    def test_default_does_not_retain_or_render_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = core.parse_rollout(fixture(home))
            self.assertIsNone(session.details)
            self.assertIsNone(session.cwd)
            payload = report.build_payload(core.aggregate([session]), home)
            blob = json.dumps(payload, ensure_ascii=False)
            page = report.render_html(payload)
            self.assertNotIn(SECRET, blob + page)
            self.assertNotIn(ATTACK, blob + page)
            self.assertEqual(payload['privacy'], 'metadata_only')
            self.assertEqual(session.totals['input'], 2000)
            self.assertEqual(session.latest.context_size, 500)
            self.assertEqual(session.compaction_count, 1)
            self.assertEqual(session.timing.turns[0].time_to_first_token_ms, 230)
            self.assertEqual(session.context_changes()[-1]['change'], 100)

    def test_detail_summary_is_computed_in_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session = core.parse_rollout(fixture(home), detailed=True)
            aggregate = core.aggregate([session])
            summary = aggregate.detail_summary()
            self.assertEqual((summary['commands'], summary['failed'], summary['slow'], summary['messages']), (1, 1, 1, 1))
            self.assertEqual(summary['programs'], [{'name': 'echo', 'count': 1}])
            self.assertTrue(any(row['kind'] == 'write' and row['count'] == 1 for row in summary['files']))
            payload = report.build_payload(aggregate, home, detailed=True)
            self.assertEqual(payload['detail_summary'], summary)
            page = report.render_html(payload)
            self.assertIn('已记录 1 条，已知失败 1 条，已知慢 1 条', page)
            self.assertIn('data-tags="失败 慢"', page)
            self.assertNotIn(ATTACK, page)

    def test_unknown_event_object_never_leaks_private_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rollout-malformed.jsonl'
            path.write_text(json.dumps(line('event_msg', {'type': {'secret': SECRET}})) + '\n', encoding='utf-8')
            session = core.parse_rollout(path)
            payload = report.build_payload(core.aggregate([session]), Path(tmp))
            self.assertEqual(session.unknown_lines, {'unknown': 1})
            self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))
            self.assertNotIn(SECRET, report.render_html(payload))

    def test_rejection_happens_before_loading(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(report, 'request_detailed_consent', return_value=False), mock.patch.object(report, 'load_sessions') as load:
            out = Path(tmp) / 'denied.html'
            self.assertEqual(report.main(['--detailed', '--codex-home', tmp, '--out', str(out)]), 2)
            load.assert_not_called()
            self.assertFalse(out.exists())

    def test_approved_report_escapes_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            fixture(home)
            out = Path(tmp) / 'detail.html'
            jout = Path(tmp) / 'detail.json'
            with mock.patch.object(report, 'request_detailed_consent', return_value=True) as consent:
                self.assertEqual(report.main(['--detailed', '--codex-home', str(home), '--out', str(out), '--json', str(jout)]), 0)
                self.assertIn('最近 30 个会话', consent.call_args.args[0])
            page = out.read_text(encoding='utf-8')
            data = json.loads(jout.read_text(encoding='utf-8'))
            self.assertEqual(data['privacy'], 'detailed')
            self.assertIn(SECRET, page)
            self.assertNotIn(ATTACK, page)
            self.assertIn('&lt;img src=x onerror=', page)
            self.assertIn('Content-Security-Policy', page)
            for section in ('记录浏览器', '上下文事件', '文件活动', '命令执行', '编队'):
                self.assertIn(section, page)
            detail = data['sessions'][0]['details']
            self.assertEqual(detail['commands'][0]['duration'], 15)
            self.assertEqual(detail['commands'][0]['exit_code'], 2)
            self.assertEqual(detail['commands'][0]['program'], 'echo')
            self.assertTrue(any(f['kind'] == 'write' for f in detail['files']))
            self.assertTrue(any(c['kind'] == 'message' for c in detail['collaboration']))
            if os.name == 'posix':
                self.assertEqual(out.stat().st_mode & 0o777, 0o600)
                self.assertEqual(jout.stat().st_mode & 0o777, 0o600)
            visual = os.environ.get('CODEX_CONTEXT_VISUAL_REPORT')
            if visual:
                report.write_private(Path(visual), page)

    def test_real_pipe_cannot_consent(self):
        if os.name != 'posix':
            self.skipTest('POSIX headless stdin check')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'forbidden.html'
            env = dict(os.environ, DISPLAY='')
            proc = subprocess.run([sys.executable, str(Path(report.__file__)), '--detailed',
                                   '--codex-home', tmp, '--out', str(out)],
                                  input='YES\n', text=True, capture_output=True, env=env, timeout=15)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(out.exists())

    def test_output_cannot_overwrite_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = fixture(home)
            before = path.read_bytes()
            with self.assertRaises(SystemExit):
                report.main(['--codex-home', tmp, '--out', str(path)])
            self.assertEqual(path.read_bytes(), before)

    def test_canonical_usage_fallback_and_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rollout-test.jsonl'
            usage = {'input_tokens': 400, 'output_tokens': 20, 'total_tokens': 420}
            path.write_text('\n'.join(json.dumps(x) for x in [
                line('session_meta', {'id': SID}),
                line('token_usage_record', {'thread_id': SID, 'usage': usage,
                     'thread_token_usage': {'input_tokens': 900, 'output_tokens': 100, 'total_tokens': 1000}}),
                line('response_item', {'type': {'unexpected': SECRET}}),
                line('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 'NaN'}}}),
            ]) + '\n', encoding='utf-8')
            s = core.parse_rollout(path, detailed=True)
            self.assertEqual(s.totals['input'], 900)
            self.assertEqual(s.latest.context_size, 400)
            self.assertIsNone(core._as_float(float('nan')))
            self.assertEqual(core._as_int('Infinity'), 0)
            self.assertIsNone(core.ToolTiming('x', started_at=10, completed_at=5).duration)

    def test_installer_dry_run_has_no_filesystem_writes(self):
        import importlib.util
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location('context_installer', root / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            source.mkdir()
            (source / 'plugin.json').write_text('{}', encoding='utf-8')
            dest = Path(tmp) / 'not-created'
            installer.copy_payload(source, dest, dry_run=True)
            self.assertFalse(dest.exists())
            market = Path(tmp) / 'marketplace.json'
            market.write_text('{bad', encoding='utf-8')
            installer.read_marketplace(market, dry_run=True)
            self.assertFalse(market.with_suffix('.json.bak-codex-context').exists())

    def test_mcp_stdio_protocol_with_synthetic_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture(Path(tmp))
            env = dict(os.environ, CODEX_HOME=tmp)
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'resources/read', 'params': {'uri': mcp_server.URI}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'context_dashboard', 'arguments': {}}},
            ]
            proc = subprocess.run([sys.executable, mcp_server.__file__], input='\n'.join(json.dumps(x) for x in requests) + '\n',
                                  text=True, capture_output=True, env=env, timeout=15, check=True)
            responses = [json.loads(line) for line in proc.stdout.splitlines()]
            self.assertEqual(len(responses), 4)
            self.assertEqual(responses[0]['result']['protocolVersion'], '2025-06-18')
            self.assertEqual(responses[3]['result']['_meta']['context-insight']['sessions'], 1)
            self.assertNotIn(SECRET, proc.stdout + proc.stderr)
            self.assertNotIn('/synthetic', proc.stdout + proc.stderr)

    def test_mcp_statistics_are_ui_only_and_no_details_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fixture(home)
            with mock.patch.object(mcp_server, 'codex_home', return_value=home):
                result = mcp_server.dispatch('tools/call', {'name': 'context_dashboard', 'arguments': {}})
            self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))
            self.assertNotIn('/synthetic', json.dumps(result, ensure_ascii=False))
            self.assertEqual(result['_meta']['context-insight']['sessions'], 1)
            self.assertNotIn('2000', json.dumps(result['content']))
            tool = mcp_server.dispatch('tools/list', {})['tools'][0]
            self.assertEqual(tool['_meta']['ui']['resourceUri'], mcp_server.URI)
            self.assertEqual({x['type'] for x in tool['_meta']['openai/ui']['entrypoints']}, {'global', 'thread'})
            with self.assertRaises(ValueError):
                mcp_server.dispatch('tools/call', {'name': 'details', 'arguments': {}})


if __name__ == '__main__':
    unittest.main()
