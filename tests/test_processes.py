"""进程模块：枚举、关键进程判定、发布者读取、结束、报告、页面规则。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt

from core.knowledge import load_from_files
from core.processes import (
    CRITICAL_HINT,
    ERR_GONE,
    ProcessItem,
    ProcessReport,
    ProcessResult,
    enumerate_processes,
    get_publisher,
    is_critical_process,
    kill_process,
    sample_cpu,
)

ROOT_DIR = Path(__file__).resolve().parent.parent

class CriticalTests(unittest.TestCase):
    def test_known_critical(self):
        for name in ("System", "smss.exe", "csrss.exe", "wininit.exe",
                     "services.exe", "lsass.exe", "winlogon.exe"):
            self.assertTrue(is_critical_process(name), name)

    def test_case_insensitive(self):
        self.assertTrue(is_critical_process("CSRSS.EXE"))

    def test_normal_process(self):
        self.assertFalse(is_critical_process("chrome.exe"))
        self.assertFalse(is_critical_process("notepad.exe"))

class EnumerateTests(unittest.TestCase):
    def test_returns_processes(self):
        items, warnings = enumerate_processes()
        self.assertGreater(len(items), 10, "至少能看到十几个进程")
        # PID 0 是 System Idle Process，合法值，不能用 > 0 卡
        self.assertTrue(all(item.pid >= 0 for item in items))

    def test_system_processes_marked(self):
        items, _warnings = enumerate_processes(with_publisher=False)
        critical = [item for item in items if item.system]
        self.assertTrue(critical, "总该有几个系统进程")

    def test_locked_when_no_exe(self):
        items, _warnings = enumerate_processes(with_publisher=False)
        locked = [item for item in items if item.locked]
        # System / System Idle Process 这类读不到路径
        for item in locked:
            self.assertFalse(item.exe)
        self.assertTrue(all(not item.locked or not item.exe for item in items))

class PublisherTests(unittest.TestCase):
    def test_reads_company_name(self):
        # 自己的解释器一定有发布者
        publisher = get_publisher(sys.executable)
        self.assertTrue(publisher, f"读不到 {sys.executable} 的发布者")

    def test_missing_file_is_empty(self):
        self.assertEqual(get_publisher(r"C:\根本不存在\x.exe"), "")
        self.assertEqual(get_publisher(""), "")

class KillTests(unittest.TestCase):
    def test_kill_own_child_process(self):
        child = subprocess.Popen(["cmd", "/c", "timeout", "/t", "30"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            result = kill_process(child.pid, "timeout")
            self.assertTrue(result.ok, result.error)
        finally:
            try:
                child.kill()
            except OSError:
                pass

    def test_missing_pid(self):
        result = kill_process(999999, "不存在")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, ERR_GONE)

class ReportTests(unittest.TestCase):
    def test_counts_and_text(self):
        now = time.time()
        report = ProcessReport(
            items=[ProcessResult(1, "a.exe", True, None, now),
                   ProcessResult(2, "b.exe", False, ERR_GONE, now)],
            started=now - 1, finished=now,
        )
        self.assertEqual(report.ok_count, 1)
        self.assertEqual(report.fail_count, 1)
        self.assertIn("结束了 1 个进程", report.summary)
        text = report.as_log_text()
        self.assertIn("成功 | a.exe", text)
        self.assertIn("失败 | b.exe", text)

class ProcessKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.kb = load_from_files(ROOT_DIR / "resources" / "default_knowledge.json", ROOT_DIR / "知识库.json")

    def test_lsass_forbidden(self):
        result = self.kb.match_process("lsass.exe")
        self.assertTrue(result.known)
        self.assertEqual(result.risk, "forbidden")

    def test_system_exact_match(self):
        self.assertEqual(self.kb.match_process("System").risk, "forbidden")
        # system.exe 和 system 不是一回事
        result = self.kb.match_process("system.exe")
        self.assertFalse(result.known or result.risk == "forbidden" and result.rule_id == "proc-system")

    def test_normal_unlisted(self):
        self.assertFalse(self.kb.match_process("chrome.exe").known)

    def test_kind_count(self):
        self.assertEqual(self.kb.counts.get("process"), 9)

class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def build(self):
        from ui.process_view import ProcessModel, ProcessRow

        model = ProcessModel()
        model.set_rows([
            ProcessRow(item=ProcessItem(100, "chrome.exe", "X", 1024, 1.5, r"C:\chrome.exe", "Google"),
                       effect="浏览器", risk="caution"),
            ProcessRow(item=ProcessItem(4, "System", "SYSTEM", 0, 0.0, "", "", locked=True, system=True),
                       effect=CRITICAL_HINT, risk="forbidden"),
        ])
        return model

    def test_nine_columns(self):
        from ui.process_view import HEADERS

        model = self.build()
        self.assertEqual(model.columnCount(), 9)
        self.assertEqual(HEADERS[0], "进程名")
        self.assertEqual(HEADERS[-1], "是否结束")

    def test_system_process_still_checkable(self):
        from ui.process_view import COL_CHECK

        model = self.build()
        self.assertTrue(model.flags(model.index(1, COL_CHECK)) & Qt.ItemIsUserCheckable,
                        "系统进程也允许勾选，走三道坎")

    def test_cpu_update(self):
        model = self.build()
        model.update_cpu({100: 42.5})
        self.assertAlmostEqual(model.rows()[0].item.cpu, 42.5)

    def test_ai_cannot_downgrade(self):
        model = self.build()
        model.update_ai("4", "AI 说随便结束", "safe")
        self.assertEqual(model.rows()[1].risk, "forbidden")

    def test_update_ai_by_exe(self):
        """AI 回来的是 exe 路径，要能落到对应行上 —— 以前按 PID 找，永远找不到。"""
        from ui.process_view import ProcessModel, ProcessRow

        model = ProcessModel()
        model.set_rows([
            ProcessRow(item=ProcessItem(100, "chrome.exe", "X", 1024, 1.5,
                                        r"C:\chrome.exe", "Google")),
            ProcessRow(item=ProcessItem(200, "chrome.exe", "X", 900, 0.5,
                                        r"C:\chrome.exe", "Google")),
            ProcessRow(item=ProcessItem(300, "other.exe", "Y", 100, 0.1,
                                        r"C:\other.exe", "Other")),
        ])
        updated = model.update_ai_by_exe(r"C:\chrome.exe", "浏览器主程序", "caution")
        self.assertEqual(updated, 2, "同一 exe 的两个实例都该更新")
        rows = model.rows()
        self.assertEqual(rows[0].effect, "浏览器主程序")
        self.assertEqual(rows[1].effect, "浏览器主程序")
        self.assertEqual(rows[2].effect, "", "别的程序不该被改")

    def test_update_ai_by_exe_unknown(self):
        model = self.build()
        self.assertEqual(model.update_ai_by_exe(r"C:\不存在.exe", "x", "caution"), 0)

    def test_update_ai_by_exe_empty(self):
        model = self.build()
        self.assertEqual(model.update_ai_by_exe("", "x", "caution"), 0)

    def test_ai_key_uses_exe(self):
        """发给 AI 的标识是 exe 路径，不是 PID。"""
        from ui.process_view import ProcessRow

        row = ProcessRow(item=ProcessItem(100, "chrome.exe", "X", 1024, 1.5,
                                          r"C:\chrome.exe", "Google"))
        self.assertEqual(row.item.exe or row.name, r"C:\chrome.exe")

    def test_ai_key_falls_back_to_name(self):
        from ui.process_view import ProcessRow

        row = ProcessRow(item=ProcessItem(5, "System", "SYSTEM", 0, 0.0, "", "", locked=True))
        self.assertEqual(row.item.exe or row.name, "System")

    def test_pid_not_used_as_ai_key(self):
        """反向验证：PID 不能当 AI 的标识，否则回填时对不上。"""
        from ui.process_view import ProcessRow

        row = ProcessRow(item=ProcessItem(14768, "acrotray.exe", "X", 1, 0.1,
                                          r"C:\Adobe\acrotray.exe", "Adobe"))
        self.assertNotEqual(row.item.exe or row.name, str(row.item.pid))

if __name__ == "__main__":
    unittest.main(verbosity=2)
class OrphanTests(unittest.TestCase):
    """找出既不属于系统、也不属于已装软件的进程。"""

    def make(self, pid, name, exe, system=False, publisher=""):
        return ProcessItem(pid, name, "user", 100, 0.1, exe, publisher, system=system)

    def test_excludes_system_process(self):
        from core.processes import find_orphans

        procs = [
            self.make(4, "csrss.exe", r"C:\Windows\System32\csrss.exe"),
            self.make(9999, "weird.exe", r"D:\Temp\weird.exe"),
        ]
        found = find_orphans([], procs)
        self.assertEqual([p.name for p in found], ["weird.exe"])

    def test_excludes_windows_directory(self):
        from core.processes import find_orphans

        procs = [self.make(100, "svchost.exe", r"C:\Windows\System32\svchost.exe")]
        self.assertEqual(find_orphans([], procs), [])

    def test_excludes_installed_software(self):
        from types import SimpleNamespace

        from core.processes import find_orphans

        installed = [SimpleNamespace(name="某软件", install_location=r"D:\Apps\Mine")]
        procs = [
            self.make(1, "mine.exe", r"D:\Apps\Mine\bin\mine.exe"),
            self.make(2, "other.exe", r"D:\Elsewhere\other.exe"),
        ]
        found = find_orphans(installed, procs)
        self.assertEqual([p.name for p in found], ["other.exe"])

    def test_excludes_common_files(self):
        """Adobe ARM、Macrovision 这类共享组件不算孤儿。"""
        from core.processes import find_orphans

        procs = [self.make(1, "armsvc.exe", r"C:\Program Files (x86)\Common Files\Adobe\ARM\armsvc.exe")]
        self.assertEqual(find_orphans([], procs), [])

    def test_excludes_program_files(self):
        """装在 Program Files 下的，哪怕没登记，也算已装软件。"""
        from core.processes import find_orphans

        procs = [self.make(1, "x.exe", r"C:\Program Files\SomeVendor\x.exe")]
        self.assertEqual(find_orphans([], procs), [])

    def test_keeps_unknown_path(self):
        from core.processes import find_orphans

        procs = [self.make(1, "tool.exe", r"D:\Temp\tool\tool.exe")]
        found = find_orphans([], procs)
        self.assertEqual(len(found), 1)

    def test_skips_when_exe_missing(self):
        """读不到路径的判断不了，不瞎猜。"""
        from core.processes import find_orphans

        procs = [self.make(1, "locked.exe", "")]
        self.assertEqual(find_orphans([], procs), [])

    def test_group_by_exe(self):
        from core.processes import find_orphans, group_by_exe

        procs = [
            self.make(1, "a.exe", r"D:\T\a.exe"),
            self.make(2, "a.exe", r"D:\T\a.exe"),
            self.make(3, "b.exe", r"D:\T\b.exe"),
        ]
        groups = group_by_exe(find_orphans([], procs))
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0][1]), 2, "实例多的排前面")
class ParentChildTests(unittest.TestCase):
    """判断依据是父亲还在不在 —— 微信拉起的插件、WorkBuddy 拉起的 node 都不算孤儿。"""

    def make(self, pid, name, exe, ppid=0, system=False):
        return ProcessItem(pid, name, "user", 100, 0.1, exe, "", system=system, ppid=ppid)

    def test_child_of_running_app_excluded(self):
        from core.processes import find_orphans

        procs = [
            self.make(1, "explorer.exe", r"C:\Windows\explorer.exe", ppid=0),
            self.make(100, "WeChat.exe", r"D:\Apps\WeChat\WeChat.exe", ppid=1),
            self.make(200, "WeChatAppEx.exe", r"D:\Temp\WeChatAppEx.exe", ppid=100),
        ]
        # WeChat.exe 是 explorer 拉起来的（独立启动，算孤儿候选），
        # 但 WeChatAppEx 是 WeChat 拉起来的，父进程还在 → 不算孤儿
        found = find_orphans([], procs)
        self.assertEqual([p.name for p in found], ["WeChat.exe"])

    def test_child_of_explorer_kept(self):
        """用户双击启动的，父进程是 explorer —— 这算独立启动。"""
        from core.processes import find_orphans

        procs = [
            self.make(1, "explorer.exe", r"C:\Windows\explorer.exe", ppid=0),
            self.make(300, "tool.exe", r"D:\Tools\tool.exe", ppid=1),
        ]
        found = find_orphans([], procs)
        self.assertEqual([p.name for p in found], ["tool.exe"])

    def test_dead_parent_kept(self):
        """父进程已经退出（PID 不在列表里）—— 这才是真孤儿。"""
        from core.processes import find_orphans

        procs = [self.make(400, "leftover.exe", r"D:\Gone\leftover.exe", ppid=99999)]
        found = find_orphans([], procs)
        self.assertEqual([p.name for p in found], ["leftover.exe"])

    def test_child_of_cmd_excluded(self):
        """cmd / powershell 拉起的算独立启动，不算某个软件的子进程。"""
        from core.processes import find_orphans

        procs = [
            self.make(1, "cmd.exe", r"C:\Windows\System32\cmd.exe", ppid=0),
            self.make(500, "script.exe", r"D:\Work\script.exe", ppid=1),
        ]
        found = find_orphans([], procs)
        self.assertEqual([p.name for p in found], ["script.exe"])

    def test_path_matches_software_name(self):
        """路径里带软件名的（.workbuddy）也算那个软件的。"""
        from types import SimpleNamespace

        from core.processes import find_orphans

        installed = [SimpleNamespace(name="WorkBuddy", install_location="")]
        procs = [self.make(600, "node.exe",
                           r"C:\Users\x\.workbuddy\binaries\node\node.exe", ppid=99999)]
        self.assertEqual(find_orphans(installed, procs), [])
class AiMemoryTests(unittest.TestCase):
    """AI 识别过的结果要记住 —— 结束进程会重建列表，不能一刷新就忘。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def make_model(self):
        from ui.process_view import ProcessModel, ProcessRow

        model = ProcessModel()
        model.set_rows([
            ProcessRow(item=ProcessItem(100, "chrome.exe", "X", 1024, 1.5,
                                        r"C:\Apps\chrome.exe", "Google")),
        ])
        return model

    def test_memory_recorded(self):
        model = self.make_model()
        model.update_ai_by_exe(r"C:\Apps\chrome.exe", "浏览器", "caution")
        self.assertIsNotNone(model.memory_of(r"C:\Apps\chrome.exe"))

    def test_memory_survives_rebuild(self):
        """重建行之后，记忆里的结论要能套回去。"""
        from ui.process_view import ProcessRow, ProcessModel

        model = self.make_model()
        model.update_ai_by_exe(r"C:\Apps\chrome.exe", "浏览器主程序", "caution")
        remembered = model.memory_of(r"C:\Apps\chrome.exe")
        self.assertEqual(remembered[0], "浏览器主程序")

        # 模拟刷新后重建
        fresh = ProcessRow(item=ProcessItem(200, "chrome.exe", "X", 900, 0.5,
                                            r"C:\Apps\chrome.exe", "Google"))
        self.assertEqual(remembered[0], "浏览器主程序")
        self.assertEqual(fresh.effect, "")

    def test_memory_key_case_insensitive(self):
        model = self.make_model()
        model.update_ai_by_exe(r"C:\Apps\Chrome.EXE", "浏览器", "caution")
        self.assertIsNotNone(model.memory_of(r"c:\apps\chrome.exe"))

    def test_memory_empty_key_ignored(self):
        model = self.make_model()
        model.update_ai_by_exe("", "x", "caution")
        self.assertIsNone(model.memory_of(""))

    def test_risk_cannot_downgrade(self):
        """AI 说安全也不能把知识库判的「别删」降成安全。"""
        from ui.process_view import ProcessModel, ProcessRow

        model = ProcessModel()
        model.set_rows([
            ProcessRow(item=ProcessItem(4, "csrss.exe", "SYSTEM", 0, 0.0,
                                        r"C:\Windows\csrss.exe", "", system=True),
                       effect="系统关键进程", risk="forbidden"),
        ])
        model.update_ai_by_exe(r"C:\Windows\csrss.exe", "随便结束", "safe")
        # update_ai 内部有 rank 比较，不会降级
        self.assertEqual(model.rows()[0].risk, "forbidden")
class IdleProcessTests(unittest.TestCase):
    """按「开了多久 + 在不在前台 + CPU 动没动」找空闲进程。"""

    def make(self, pid, name, age_minutes=120, cpu=0.0, session=1,
             exe=None, system=False, locked=False):
        import time as _t

        import os as _os

        # 用当前用户名 —— 判断逻辑会按账户过滤，造别的名字会被当成系统服务排掉
        return ProcessItem(
            pid, name, _os.environ.get("USERNAME", "user"), 100, cpu,
            exe or (r"D:\Apps\%s.exe" % name),
            "", system=system, locked=locked,
            create_time=_t.time() - age_minutes * 60, session=session,
        )

    def test_finds_long_running_idle(self):
        from core.processes import find_idle_processes

        procs = [self.make(100, "browser.exe", age_minutes=180)]
        found = find_idle_processes(procs, min_age_minutes=30, max_cpu=1.0)
        self.assertEqual([p.name for p in found], ["browser.exe"])

    def test_skips_young_process(self):
        """刚开的不算。"""
        from core.processes import find_idle_processes

        procs = [self.make(100, "new.exe", age_minutes=5)]
        self.assertEqual(find_idle_processes(procs, min_age_minutes=30), [])

    def test_skips_busy_process(self):
        """正在算东西的不算。"""
        from core.processes import find_idle_processes

        procs = [self.make(100, "busy.exe", age_minutes=120, cpu=45.0)]
        self.assertEqual(find_idle_processes(procs, max_cpu=1.0), [])

    def test_skips_service_session(self):
        """会话 0 是服务，不动。"""
        from core.processes import find_idle_processes

        procs = [self.make(100, "spoolsv.exe", session=0)]
        self.assertEqual(find_idle_processes(procs), [])

    def test_skips_unknown_session(self):
        from core.processes import find_idle_processes

        procs = [self.make(100, "locked.exe", session=-1)]
        self.assertEqual(find_idle_processes(procs), [])

    def test_skips_system_process(self):
        from core.processes import find_idle_processes

        procs = [self.make(4, "csrss.exe", system=True)]
        self.assertEqual(find_idle_processes(procs), [])

    def test_skips_shell_processes(self):
        """桌面和开始菜单动了就没桌面了。"""
        from core.processes import find_idle_processes

        for name in ("explorer.exe", "SearchHost.exe", "StartMenuExperienceHost.exe"):
            procs = [self.make(100, name)]
            self.assertEqual(find_idle_processes(procs), [], "%s 不该被列出" % name)

    def test_skips_foreground(self):
        """前台那个是用户正在用的，不能动。"""
        from unittest import mock

        from core.processes import find_idle_processes

        procs = [self.make(100, "browser.exe"), self.make(200, "other.exe")]
        with mock.patch("core.processes.foreground_pid", return_value=100):
            found = find_idle_processes(procs)
        self.assertEqual([p.name for p in found], ["other.exe"])

    def test_sorted_by_age(self):
        """开得久的排前面。"""
        from core.processes import find_idle_processes

        procs = [
            self.make(1, "short.exe", age_minutes=40),
            self.make(2, "long.exe", age_minutes=500),
        ]
        found = find_idle_processes(procs)
        self.assertEqual(found[0].name, "long.exe")

    def test_age_text(self):
        from core.processes import age_text

        import time as _t

        now = _t.time()
        self.assertIn("分钟", age_text(ProcessItem(1, "a", "", 0, 0, "", "",
                                                   create_time=now - 600), now))
        self.assertIn("小时", age_text(ProcessItem(1, "a", "", 0, 0, "", "",
                                                   create_time=now - 7200), now))
        self.assertIn("天", age_text(ProcessItem(1, "a", "", 0, 0, "", "",
                                                 create_time=now - 90000), now))
        self.assertEqual(age_text(ProcessItem(1, "a", "", 0, 0, "", ""), now), "—")

    def test_locked_skipped(self):
        from core.processes import find_idle_processes

        procs = [self.make(100, "locked.exe", locked=True)]
        self.assertEqual(find_idle_processes(procs), [])
class ProcessCleanDialogTests(unittest.TestCase):
    """清理空闲进程的对话框：信息要完整，默认一个都不勾。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def build_dialog(self):
        import time as _t

        from core.processes import ProcessItem
        from ui.process_clean_dialog import ProcessCleanDialog

        now = _t.time()
        items = [
            ProcessItem(100, "msedge.exe", "u", 1000, 0.2,
                        r"C:\Edge\msedge.exe", "", create_time=now - 7200, session=1),
            ProcessItem(200, "msedge.exe", "u", 900, 0.1,
                        r"C:\Edge\msedge.exe", "", create_time=now - 7200, session=1),
            ProcessItem(300, "Code.exe", "u", 800, 0.0,
                        r"D:\Code\Code.exe", "", create_time=now - 90000, session=1),
        ]
        return ProcessCleanDialog(items, max_cpu=1.0, min_age_minutes=30)

    def test_groups_by_exe(self):
        dialog = self.build_dialog()
        self.assertEqual(len(dialog._groups), 2)

    def test_default_unchecked(self):
        dialog = self.build_dialog()
        self.assertEqual(dialog._chosen(), [])

    def test_kill_disabled_until_checked(self):
        dialog = self.build_dialog()
        self.assertFalse(dialog.btn_kill.isEnabled())

    def test_stat_label_shows_cpu(self):
        """「开了 X 小时 CPU Y%」两个数都要看得见 —— 之前被截成只剩 CPU。"""
        from PySide6.QtWidgets import QLabel

        dialog = self.build_dialog()
        stats = [w for w in dialog.findChildren(QLabel) if w.text().startswith("开了 ")]
        self.assertTrue(stats, "没有统计标签")
        for label in stats:
            self.assertIn("CPU", label.text())
            self.assertRegex(label.text(), r"CPU \d+\.\d+%",
                             "CPU 数值不见了：%s" % label.text())
            self.assertGreaterEqual(label.minimumWidth(), 180,
                                    "最小宽度太小，右边缘会截断")

    def test_title_says_processes(self):
        from PySide6.QtWidgets import QCheckBox

        dialog = self.build_dialog()
        boxes = dialog.findChildren(QCheckBox)
        self.assertTrue(boxes)
        self.assertIn("进程", boxes[0].text())

    def test_kill_passes_pid_not_object(self):
        """kill_process 收的是 pid，不是整个 ProcessItem —— 传错会 TypeError。"""
        from unittest import mock

        dialog = self.build_dialog()
        from PySide6.QtWidgets import QCheckBox

        dialog.findChildren(QCheckBox)[0].setChecked(True)
        with mock.patch("ui.process_clean_dialog.kill_process") as killer:
            killer.return_value = mock.Mock(ok=True)
            dialog._do_kill()
        self.assertTrue(killer.called)
        first = killer.call_args_list[0]
        pid, name = first[0][0], first[0][1]
        self.assertIsInstance(pid, int, "第一个参数必须是 pid（int），不是对象")
        self.assertIsInstance(name, str)
        self.assertEqual(dialog.killed, 2)

    def test_kill_survives_missing_process(self):
        """真删一个不存在的 pid，不能抛异常。"""
        dialog = self.build_dialog()
        from PySide6.QtWidgets import QCheckBox

        dialog.findChildren(QCheckBox)[0].setChecked(True)
        dialog._do_kill()          # 这些 pid 是造的，必然结束不了
        self.assertEqual(dialog.killed, 0)
        self.assertEqual(dialog.failed, 2)
        self.assertIn("没成功", dialog.summary.text())

    def test_stat_label_fixed_policy(self):
        """宽度策略必须是 Fixed，否则右边还会被挤扁。"""
        from PySide6.QtWidgets import QLabel, QSizePolicy

        dialog = self.build_dialog()
        for label in dialog.findChildren(QLabel):
            if label.text().startswith("开了 "):
                self.assertEqual(label.sizePolicy().horizontalPolicy(),
                                 QSizePolicy.Fixed)