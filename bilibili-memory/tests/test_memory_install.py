from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
import install_amemorix  # noqa: E402


class MemoryInstallTests(unittest.TestCase):
    def test_reports_would_install_without_changing_environment(self) -> None:
        with mock.patch.object(install_amemorix, "module_available", return_value=False), \
             mock.patch.object(install_amemorix, "package_version", return_value=None), \
             mock.patch.object(install_amemorix.subprocess, "run") as run:
            result = install_amemorix.ensure_amemorix("MaiBot>=1.3.15,<1.4", apply=False)

        self.assertEqual(result["action"], "would-install")
        self.assertFalse(result["applied"])
        run.assert_not_called()

    def test_installs_mai_bot_and_detects_amemorix(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="installed", stderr="")
        with mock.patch.object(install_amemorix, "module_available", side_effect=[False, True]), \
             mock.patch.object(install_amemorix, "package_version", return_value="1.3.17"), \
             mock.patch.object(install_amemorix.subprocess, "run", return_value=completed) as run:
            result = install_amemorix.ensure_amemorix("MaiBot>=1.3.15,<1.4", apply=True)

        self.assertEqual(result["action"], "installed")
        self.assertTrue(result["applied"])
        self.assertTrue(result["aMemorixInstalled"])
        command = run.call_args.args[0]
        self.assertIn("pip", command)
        self.assertIn("MaiBot>=1.3.15,<1.4", command)

    def test_repairs_same_version_when_module_is_still_missing(self) -> None:
        installed = subprocess.CompletedProcess([], 0, stdout="upgraded", stderr="")
        repaired = subprocess.CompletedProcess([], 0, stdout="repaired", stderr="")
        with mock.patch.object(install_amemorix, "module_available", side_effect=[False, False, True]), \
             mock.patch.object(install_amemorix, "package_version", return_value="1.3.15"), \
             mock.patch.object(install_amemorix.subprocess, "run", side_effect=[installed, repaired]) as run:
            result = install_amemorix.ensure_amemorix("MaiBot>=1.3.15,<1.4", apply=True)

        self.assertEqual(result["action"], "repaired")
        self.assertTrue(result["applied"])
        repair_command = run.call_args_list[1].args[0]
        self.assertIn("--force-reinstall", repair_command)
        self.assertIn("MaiBot==1.3.15", repair_command)

    def test_failed_pip_is_reported_with_output(self) -> None:
        failed = subprocess.CompletedProcess([], 2, stdout="network error", stderr="pip failed")
        with mock.patch.object(install_amemorix, "module_available", return_value=False), \
             mock.patch.object(install_amemorix, "package_version", return_value="1.3.15"), \
             mock.patch.object(install_amemorix.subprocess, "run", return_value=failed):
            result = install_amemorix.ensure_amemorix("MaiBot>=1.3.15,<1.4", apply=True)

        self.assertEqual(result["action"], "install-failed")
        self.assertFalse(result["aMemorixInstalled"])
        self.assertIn("pip exited with code 2", result["error"])
        self.assertIn("pip failed", result["output"])


if __name__ == "__main__":
    unittest.main()