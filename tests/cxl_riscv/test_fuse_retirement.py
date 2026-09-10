"""Synchronize the unmount/handler-removal boundary without timing a race."""
from pathlib import Path
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).parents[2] / 'deploy/cxl-riscv'))
import full_stack


class FuseRetirementTest(unittest.TestCase):
    def test_unmounted_process_finishes_cleanup_without_second_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('unmount', 'ack', 'cleanup'):
                os.mkfifo(root / name)
            child = root / 'participant.py'
            child.write_text('''from pathlib import Path
import os,signal,sys
root=Path(sys.argv[1])
signal.alarm(4)
signal.signal(signal.SIGTERM, lambda *_: None)
with (root/'unmount').open() as stream: stream.read(1)
# FuseMainLoop removes signal handlers before FuseClients::stop retires CXL.
signal.signal(signal.SIGTERM, signal.SIG_DFL)
gate=os.open(root/'cleanup', os.O_RDWR)
with (root/'ack').open('w') as stream: stream.write('ready\\n')
os.read(gate,1)
os.close(gate)
(root/'retired').write_text('retired')
''')
            # The unmount helper returns only after signal-handler removal.
            # The first wait-loop iteration then permits participant cleanup.
            script = f'''{shlex.quote(sys.executable)} {child} {root} & owned=$!
                grep() {{ return 0; }}
                fusermount3() {{ printf U >{root}/unmount; read -r ack <{root}/ack; }}
                sleep() {{ if [ ! -f {root}/released ]; then
                    touch {root}/released; {shlex.quote(sys.executable)} -c '
import os
try:
 fd=os.open("{root}/cleanup",os.O_WRONLY|os.O_NONBLOCK)
 os.write(fd,b"C");os.close(fd)
except OSError:pass
'; fi; /bin/sleep 0.01; }}
                {full_stack.application_stop_command('hf3fs_fuse_main', 'owned', 1, True)}
                test -f {root}/retired
            '''
            result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True, timeout=6)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('rc=0', result.stdout)


if __name__ == '__main__':
    unittest.main()
