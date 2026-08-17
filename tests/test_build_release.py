from __future__ import annotations

import subprocess
from pathlib import Path
import unittest
from unittest import mock

from tools.build_release import BuildError, run_command


class ReleaseCommandTests(unittest.TestCase):
    @mock.patch("tools.build_release.time.sleep")
    @mock.patch("tools.build_release.subprocess.run")
    def test_run_command_retries_transient_failure(
        self,
        run_mock: mock.Mock,
        sleep_mock: mock.Mock,
    ) -> None:
        command = ["ISCC.exe", "installer.iss"]
        run_mock.side_effect = (
            subprocess.CompletedProcess(command, 2),
            subprocess.CompletedProcess(command, 0),
        )

        with mock.patch("builtins.print"):
            run_command(
                command,
                cwd=Path("."),
                attempts=3,
                retry_delay_seconds=2.0,
                retry_hint="Output may be locked.",
            )

        self.assertEqual(2, run_mock.call_count)
        sleep_mock.assert_called_once_with(2.0)

    @mock.patch("tools.build_release.time.sleep")
    @mock.patch("tools.build_release.subprocess.run")
    def test_run_command_raises_after_last_attempt(
        self,
        run_mock: mock.Mock,
        sleep_mock: mock.Mock,
    ) -> None:
        command = ["ISCC.exe", "installer.iss"]
        run_mock.return_value = subprocess.CompletedProcess(command, 2)

        with mock.patch("builtins.print"):
            with self.assertRaises(BuildError):
                run_command(
                    command,
                    cwd=Path("."),
                    attempts=3,
                    retry_delay_seconds=2.0,
                )

        self.assertEqual(3, run_mock.call_count)
        self.assertEqual([mock.call(2.0), mock.call(2.0)], sleep_mock.call_args_list)


if __name__ == "__main__":
    unittest.main()
