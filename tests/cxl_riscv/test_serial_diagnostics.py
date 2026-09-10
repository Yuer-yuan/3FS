from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / 'deploy/cxl-riscv'))
import full_stack
import run_g0


class SerialDiagnosticsTest(unittest.TestCase):
    def test_diagnostic_commands_have_no_literal_line_breaks(self):
        for node in (0, 1):
            for name, command in full_stack.live_diagnostic_commands(node, 'qualification'):
                self.assertNotIn('\n', command, name)
                self.assertNotIn('\r', command, name)

    def console(self, root):
        console = run_g0.GuestConsole(['/bin/sh'], root / 'serial.log', os.environ)
        self.addCleanup(console.stop, 0.01)
        return console

    def test_marker_requires_complete_line_even_when_output_is_slow(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = self.console(Path(tmp))
            # A marker-looking substring is ordinary command output. A partial
            # rc=0 must not be accepted before the remaining digit arrives.
            with patch('run_g0.uuid.uuid4') as uuid:
                uuid.return_value.hex = 'fixed'
                output = console.shell_command(
                    "printf 'prefix HF3FS_G0_COMMAND token=fixed rc=0'; "
                    "sleep 0.02; printf '7\\n'; printf 'payload\\n'", 2)
            self.assertIn('payload', output)
            self.assertIn('rc=07', output)

    def test_reject_line_breaks_before_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = self.console(Path(tmp))
            with self.assertRaises(ValueError):
                console.shell_command("printf 'one\ntwo'", 1)
            self.assertIn('safe', console.shell_command("printf 'safe\\n'", 1))

    def test_timeout_does_not_allow_a_second_command_to_overtake(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = self.console(Path(tmp))
            with self.assertRaises(TimeoutError):
                console.shell_command('sleep 0.2', 0.01)
            with self.assertRaises(TimeoutError):
                console.shell_command("printf 'must-not-send\\n'", 0.01)
            self.assertNotIn('must-not-send', console.output)
            self.assertIn('recovered', console.shell_command("printf 'recovered\\n'", 2))

    def test_long_commands_fit_capped_uart_and_preserve_shell_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uart = root / 'uart.py'
            uart.write_text("import subprocess, sys\n"
                            "child = subprocess.Popen(['/bin/sh'], stdin=subprocess.PIPE)\n"
                            "for line in sys.stdin.buffer:\n"
                            " child.stdin.write(line.rstrip(b'\\n')[:800] + b'\\n')\n"
                            " child.stdin.flush()\n")
            console = run_g0.GuestConsole([sys.executable, str(uart)], root / 'serial.log', os.environ)
            self.addCleanup(console.stop, 0.01)
            payload = 'x' * 2200
            output = console.shell_command(f"value='{payload}'; printf 'length=%s\\n' \"${{#value}}\"", 2)
            self.assertIn('length=2200', output)
            self.assertIn('2200', console.shell_command("printf '%s\\n' \"${#value}\"", 1))


if __name__ == '__main__':
    unittest.main()

class DiagnosticTransferTest(unittest.TestCase):
    def test_capture_slow_binary_output_is_complete_and_bounded(self):
        import diagnostics
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = []
            def command(node, text, timeout):
                result = subprocess.run(['/bin/sh', '-c', text], capture_output=True, timeout=timeout, check=True)
                outputs.append(result.stdout)
                return result.stdout.decode()
            destination = root / 'export.log'
            result = diagnostics.capture(command, 0,
                "printf 'start\\n'; sleep 0.02; head -c 14000 /dev/zero; printf 'end\\n'",
                destination, guest_directory=tmp)
            self.assertEqual(result['outcome'], 'passed', result)
            self.assertEqual(destination.read_bytes(), b'start\n' + b'\0' * 14000 + b'end\n')
            self.assertTrue(all(len(output) <= 4096 for output in outputs))

    def test_truncated_or_corrupt_chunks_are_not_accepted(self):
        import diagnostics
        import subprocess
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'source'
                source.write_bytes(b'A' * 5000)
                def command(node, text, timeout):
                    output = subprocess.check_output(['/bin/sh', '-c', text]).decode()
                    if 'HF3FS_DIAG_CHUNK 2048' in output:
                        output = output.replace('QUFB', 'QkJC', 1) if corrupt else output[:-10] + '\n'
                    return output
                with self.assertRaises(ValueError):
                    diagnostics.export_file(command, 0, str(source), root / 'result')
                self.assertFalse((root / 'result').exists())

    def test_overflow_and_failed_producer_are_recorded(self):
        import diagnostics
        import subprocess
        for text in ('head -c 2049 /dev/zero', 'printf partial; false'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                def command(node, text, timeout):
                    return subprocess.check_output(['/bin/sh', '-c', text]).decode()
                result = diagnostics.capture(command, 0, text, Path(tmp) / 'result',
                                             guest_directory=tmp, max_bytes=2048)
                self.assertEqual(result['outcome'], 'failed')
                self.assertFalse(result['verified'])
                self.assertTrue(Path(result['guest_path']).exists())

class RpcTraceRecorderTest(unittest.TestCase):
    def test_native_concurrent_recorder_and_overflow_are_verifiable(self):
        import diagnostics
        import shutil
        import subprocess
        compiler = shutil.which('g++') or shutil.which('clang++')
        if compiler is None:
            self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'trace.cc'
            source.write_text(r'''
#include "common/net/RpcTrace.h"
#include <thread>
#include <vector>
int main(int argc, char **argv) {
  hf3fs::net::RpcTrace trace(argv[1], argc > 2 ? 1024 : 1048576);
  std::vector<std::thread> threads;
  for (int t = 0; t < 4; ++t) threads.emplace_back([&] {
    for (int i = 0; i < 100; ++i) trace.record(4, 2, i, "stat", 0, "detail=a\nb");
  });
  for (auto &thread : threads) thread.join();
}
''')
            subprocess.run([compiler, '-std=c++20', '-pthread', '-Werror', '-Wall', '-Wextra',
                            '-I', str(Path(__file__).parents[2] / 'src'), str(source), '-o', str(root / 'trace')], check=True)
            subprocess.run([str(root / 'trace'), str(root / 'complete.trace')], check=True)
            record = diagnostics.validate_rpc_trace(root / 'complete.trace')
            self.assertEqual(record['records'], 400)
            subprocess.run([str(root / 'trace'), str(root / 'overflow.trace'), 'overflow'], check=True)
            with self.assertRaisesRegex(ValueError, 'dropped='):
                diagnostics.validate_rpc_trace(root / 'overflow.trace')
            self.assertLessEqual((root / 'overflow.trace').stat().st_size, 1024 + 256)
            data = (root / 'complete.trace').read_bytes()
            (root / 'torn.trace').write_bytes(data[:-1])
            with self.assertRaisesRegex(ValueError, 'length/record'):
                diagnostics.validate_rpc_trace(root / 'torn.trace')

class TransportCounterCaptureTest(unittest.TestCase):
    def test_many_process_counters_are_exported_without_one_large_serial_output(self):
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / 'guest'; logs.mkdir()
            run = root / 'host'; run.mkdir()
            records = [dict(schema='hf3fs.transport-counters.v1', pid=i,
                            counters={'cxl_rpc_requests': i}, padding='x' * 500) for i in range(32)]
            (logs / 'meta.stdout').write_text(''.join('HF3FS_CXL_TRANSPORT_COUNTERS ' + json.dumps(r) + '\n'
                                                     for r in records))
            outputs = []
            def command(node, text, timeout):
                output = subprocess.run(['/bin/sh', '-c', text], capture_output=True, text=True,
                                        check=True, timeout=timeout).stdout
                outputs.append(output)
                return output
            with patch('full_stack.LOG', str(logs)):
                receipt = full_stack.capture_transport_counters(command, 0, run)
            self.assertTrue(receipt['verified'])
            self.assertGreater(receipt['bytes'], 4096)
            self.assertEqual(receipt['records'], [dict(guest=0, **r) for r in records])
            self.assertLessEqual(max(len(x.encode()) for x in outputs), 4096)
