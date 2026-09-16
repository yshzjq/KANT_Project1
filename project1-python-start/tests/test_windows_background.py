"""실제 Windows 부모 프로세스를 종료해도 분리한 작업이 계속 동작하는지 검사합니다.

Slack이나 모델을 호출하지 않습니다. 임시 작업은 종료 파일 또는 30초 제한으로 끝납니다.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Windows 프로세스 수명 검사")
class WindowsBackgroundTests(unittest.TestCase):
    def test_worker_survives_parent_termination(self):
        with tempfile.TemporaryDirectory(prefix="slack_launch_") as folder:
            root = Path(folder) / "한글 경로"
            root.mkdir()
            worker = root / "worker.py"
            worker.write_text(
                "import json, os, time\n"
                "from pathlib import Path\n"
                "root = Path(__file__).parent\n"
                "(root / 'ready').write_text(json.dumps({'pid': os.getpid(), 'marker': os.getenv('LAUNCH_TEST_MARKER')}))\n"
                "deadline = time.monotonic() + 30\n"
                "while not (root / 'stop').exists() and time.monotonic() < deadline:\n"
                "    (root / 'heartbeat').write_text(str(time.time_ns()))\n"
                "    time.sleep(0.1)\n"
                "(root / 'finished').touch()\n", encoding="utf-8",
            )
            parent_script = root / "parent.py"
            project = Path(__file__).resolve().parents[1]
            parent_script.write_text(
                "import os, sys, time\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str(project)!r})\n"
                "from slack_bot import launch_windows_process\n"
                "root = Path(__file__).parent\n"
                "launch_windows_process([sys.executable, str(root / 'worker.py')], root, "
                "{**os.environ, 'LAUNCH_TEST_MARKER': '한글 marker'})\n"
                "(root / 'launched').touch()\n"
                "time.sleep(30)\n", encoding="utf-8",
            )

            def wait_until(condition, seconds=15):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if condition():
                        return True
                    time.sleep(0.1)
                return False

            with (root / "parent.log").open("w", encoding="utf-8") as log:
                parent = subprocess.Popen(
                    [sys.executable, str(parent_script)], stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    self.assertTrue(wait_until(lambda: (root / "launched").exists()),
                                    (root / "parent.log").read_text(encoding="utf-8", errors="replace"))
                    self.assertTrue(wait_until(lambda: (root / "heartbeat").exists()))
                    info = json.loads((root / "ready").read_text())
                    self.assertEqual(info["marker"], "한글 marker")
                    parent.terminate()
                    parent.wait(timeout=5)
                    time.sleep(2)  # 부모 종료에 따른 자식 정리가 끝날 시간도 줍니다.
                    # 종료 이후 시각으로 갱신되어야 살아 있는 작업으로 인정합니다.
                    after_exit = time.time_ns()

                    def updated_after_exit():
                        value = (root / "heartbeat").read_text()
                        return bool(value) and int(value) > after_exit

                    self.assertTrue(wait_until(updated_after_exit, seconds=3))
                finally:
                    (root / "stop").touch()
                    if parent.poll() is None:
                        parent.terminate()
                        parent.wait(timeout=5)
                    if (root / "ready").exists():
                        self.assertTrue(wait_until(lambda: (root / "finished").exists(), seconds=5))


if __name__ == "__main__":
    unittest.main()
