"""Run output cap keeps the head AND the tail (F25, 2.0).

A stream over MAX_OUTPUT_BYTES used to keep only its first 1 MB, dropping the
end of the output — usually the result line, or the traceback on stderr.
"""

import sys
from unittest import mock

from django.test import SimpleTestCase, TestCase

from core.executor import (
    MAX_OUTPUT_BYTES,
    OUTPUT_HEAD_BYTES,
    _truncate_output,
    execute_run,
    remove_script_workdir,
)
from core.models import Environment, Run, Script, Workspace


class TruncateOutputTests(SimpleTestCase):
    def _big(self, lines=50_000, width=100):
        body = "".join(f"line {i:06d} " + "x" * (width - 13) + "\n" for i in range(lines))
        return "FIRST LINE\n" + body + "LAST LINE: result=42\n"

    def test_small_output_is_unchanged(self):
        self.assertEqual(_truncate_output("hello\n"), "hello\n")
        text = "y" * MAX_OUTPUT_BYTES
        self.assertIs(_truncate_output(text), text)

    def test_large_output_keeps_head_and_tail_with_a_marker(self):
        text = self._big()  # ≈ 5 MB
        out = _truncate_output(text)
        size = len(out.encode("utf-8"))
        self.assertLessEqual(size, MAX_OUTPUT_BYTES)
        self.assertGreater(size, MAX_OUTPUT_BYTES - 10_000)  # the cap is used
        self.assertTrue(out.startswith("FIRST LINE\n"))
        self.assertTrue(out.endswith("LAST LINE: result=42\n"))  # the end survives
        self.assertIn("[OUTPUT TRUNCATED:", out)
        self.assertIn(f"of {len(text.encode()):,} bytes", out)

        head, _, tail = out.partition("\n[OUTPUT TRUNCATED:")
        # ≈200 KB head, cut at a line break; the rest of the cap is tail.
        self.assertLessEqual(len(head.encode()), OUTPUT_HEAD_BYTES)
        self.assertGreater(len(head.encode()), OUTPUT_HEAD_BYTES - 4096)
        self.assertGreater(len(tail.encode()), 790_000)
        # Whole lines on both sides of the marker.
        after_marker = tail.split("]\n", 1)[1]
        self.assertTrue(after_marker.startswith("line "))
        self.assertTrue(head.endswith("\n") or head == "")

    def test_multibyte_characters_are_never_split(self):
        text = "é" * 1_500_000  # 3 MB, no line breaks
        out = _truncate_output(text)
        self.assertLessEqual(len(out.encode("utf-8")), MAX_OUTPUT_BYTES)
        self.assertNotIn("�", out)  # no replacement character at a cut
        self.assertIn("[OUTPUT TRUNCATED:", out)


class ExecutorKeepsTheTracebackTests(TestCase):
    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_huge_stderr_keeps_the_final_traceback(self, _val):
        ws = Workspace.get_default()
        env = Environment.objects.create(name="trunc-env", path="trunc-env")
        script = Script.objects.create(
            name="trunc",
            code=(
                "import sys\n"
                "for i in range(30000):\n"
                "    print('noise %05d %s' % (i, 'z' * 60), file=sys.stderr)\n"
                "raise ValueError('the real reason')\n"
            ),
            environment=env,
            workspace=ws,
        )
        self.addCleanup(remove_script_workdir, script.pk)
        run = Run.objects.create(script=script, workspace=ws)
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIn("[OUTPUT TRUNCATED:", run.stderr)
        self.assertIn("ValueError: the real reason", run.stderr)
        self.assertLessEqual(len(run.stderr.encode()), MAX_OUTPUT_BYTES)
