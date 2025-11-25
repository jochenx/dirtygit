import asyncio
import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from prompt_toolkit import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea


Branch = Tuple[str, bool]  # (branch name, is_current)


def run_git_output(args: List[str], cwd: str) -> Tuple[int, str]:
    """Run a git command and capture its output."""
    proc = subprocess.run(
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    return proc.returncode, proc.stdout


def list_branches(repo_path: str) -> List[Branch]:
    """Return a list of (branch, is_current) tuples."""
    code, current = run_git_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path
    )
    current = current.strip() if code == 0 else ""

    code, output = run_git_output(
        ["git", "for-each-ref", "refs/heads", "--format=%(refname:short)"],
        cwd=repo_path,
    )
    if code != 0:
        raise RuntimeError(output)

    branches: List[Branch] = []
    for line in output.splitlines():
        name = line.strip()
        if not name:
            continue
        branches.append((name, name == current))
    return branches


class BranchSelectorUI:
    def __init__(self, repo_path: str, quit_on_switch: bool = True) -> None:
        self.repo_path = repo_path
        self.quit_on_switch = quit_on_switch
        self.log_dir = Path.home() / ".lazygit_logging"
        self.log_dir.mkdir(exist_ok=True, parents=True)
        self.log_file = self.log_dir / "branch_selector.log"
        self.branches: List[Branch] = list_branches(repo_path)
        self.selected: int = 0
        self.processing: bool = False
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.status: str = (
            "↑/↓ to move • Enter to switch • Delete to delete • Ctrl-C/Esc to quit"
        )
        self.awaiting_force_branch: Optional[str] = None

        self.log_area = TextArea(
            text="",
            focusable=False,
            scrollbar=True,
            style="class:log",
            wrap_lines=False,
        )
        self.input_area = TextArea(
            prompt="git> ",
            height=1,
            multiline=False,
            style="class:input",
        )

        self.list_control = FormattedTextControl(
            self._render_branch_list, focusable=True
        )
        self.list_window = Window(
            content=self.list_control,
            always_hide_cursor=True,
            wrap_lines=False,
        )

        body = HSplit(
            [
                Frame(
                    self.list_window,
                    title="Branches",
                    height=D(preferred=20, max=20),
                ),
                Frame(
                    HSplit(
                        [
                            self.log_area,
                            Window(height=1, char="-", style="class:divider"),
                            self.input_area,
                        ]
                    ),
                    title="Git output (type into git> when prompted)",
                    height=D(preferred=10, max=20),
                ),
                Window(
                    height=1,
                    content=FormattedTextControl(lambda: [("class:status", self.status)]),
                ),
            ]
        )

        self.style = Style.from_dict(
            {
                "branch": "ansiwhite",
                "branch.current": "ansibrightgreen",
                "branch.selected": "ansiwhite bg:#add8e6",
                "log": "ansiwhite",
                "input": "ansiwhite",
                "status": "ansiwhite",
                "divider": "ansiwhite",
            }
        )

        kb = KeyBindings()
        self._register_keys(kb)

        self.app = Application(
            layout=Layout(body, focused_element=self.list_window),
            key_bindings=kb,
            full_screen=True,
            mouse_support=True,
            style=self.style,
        )

    def _render_branch_list(self) -> FormattedText:
        lines: FormattedText = []
        if not self.branches:
            return [("class:branch", "No branches found\n")]

        for idx, (name, is_current) in enumerate(self.branches):
            style_parts = ["class:branch"]
            if is_current:
                style_parts.append("class:branch.current")
            if idx == self.selected:
                style_parts.append("class:branch.selected")
            prefix = "* " if is_current else "  "
            lines.append((" ".join(style_parts), f"{prefix}{name}\n"))
        return lines

    def _register_keys(self, kb: KeyBindings) -> None:
        is_input_focused = Condition(
            lambda: get_app().layout.has_focus(self.input_area)
        )
        is_force_prompt = Condition(lambda: self.awaiting_force_branch is not None)

        @kb.add("c-c")
        @kb.add("escape")
        def _(event) -> None:
            event.app.exit()

        @kb.add("up", filter=~is_input_focused)
        @kb.add("k", filter=~is_input_focused)
        def _(event) -> None:
            if not self.branches:
                return
            self.selected = (self.selected - 1) % len(self.branches)

        @kb.add("down", filter=~is_input_focused)
        @kb.add("j", filter=~is_input_focused)
        def _(event) -> None:
            if not self.branches:
                return
            self.selected = (self.selected + 1) % len(self.branches)

        @kb.add("enter", filter=~is_input_focused)
        def _(event) -> None:
            if self.processing or not self.branches:
                return
            branch = self.branches[self.selected][0]
            self._run_git(
                ["git", "switch", branch],
                on_success=event.app.exit if self.quit_on_switch else self._after_switch,
                refresh=not self.quit_on_switch,
            )

        @kb.add("delete", filter=~is_input_focused)
        def _(event) -> None:
            if self.processing or not self.branches:
                return
            branch = self.branches[self.selected][0]
            self._run_git(
                ["git", "branch", "-d", branch],
                on_success=self._refresh_branches,
                refresh=True,
                on_failure=lambda: self._start_force_prompt(branch),
            )

        @kb.add("enter", filter=is_input_focused)
        def _(event) -> None:
            text = self.input_area.text
            self.input_area.buffer.reset()
            if not self.processing or not self.proc:
                return
            self.app.create_background_task(self._send_to_proc(text))
            self._append_log(f"> {text}")

        @kb.add("y", filter=is_force_prompt)
        def _(event) -> None:
            branch = self.awaiting_force_branch
            if not branch:
                return
            self.awaiting_force_branch = None
            self._append_log(f"Force deleting {branch}")
            self._run_git(
                ["git", "branch", "-D", branch],
                on_success=self._refresh_branches,
                refresh=True,
            )

        @kb.add("n", filter=is_force_prompt)
        @kb.add("enter", filter=is_force_prompt)
        def _(event) -> None:
            if not self.awaiting_force_branch:
                return
            self.awaiting_force_branch = None
            self.status = (
                "Force delete canceled. ↑/↓ to move • Enter to switch • Delete to delete"
            )
            self.app.invalidate()

    def _refresh_branches(self) -> None:
        old_name = self.branches[self.selected][0] if self.branches else None
        try:
            self.branches = list_branches(self.repo_path)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[error] {exc}")
            return

        self.selected = 0
        if old_name:
            for idx, (name, _) in enumerate(self.branches):
                if name == old_name:
                    self.selected = idx
                    break

    def _append_log(self, line: str) -> None:
        self._append_log_sync(line)

    def _append_log_sync(self, line: str) -> None:
        if self.log_area.text:
            self.log_area.text += "\n"
        self.log_area.text += line
        self.log_area.buffer.cursor_position = len(self.log_area.text)
        self._write_log_file(line)

    def _write_log_file(self, line: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] {line}\n"
        try:
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(entry)
        except Exception:
            # Avoid breaking the UI due to logging errors.
            pass

    def _run_git(
        self,
        args: List[str],
        on_success: Optional[Callable[[], None]] = None,
        on_failure: Optional[Callable[[], None]] = None,
        refresh: bool = False,
    ) -> None:
        self.processing = True
        self.status = f"Running: {' '.join(args)}"
        self._append_log(f"$ {' '.join(args)}")
        self.app.invalidate()

        self.app.layout.focus(self.input_area)
        self.app.create_background_task(
            self._run_git_async(
                args,
                on_success=on_success,
                on_failure=on_failure,
                refresh=refresh,
            )
        )

    async def _run_git_async(
        self,
        args: List[str],
        on_success: Optional[Callable[[], None]] = None,
        on_failure: Optional[Callable[[], None]] = None,
        refresh: bool = False,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.repo_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.proc = proc

        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            if isinstance(line, bytes):
                line = line.decode(errors="replace")
            self._append_log(line.rstrip("\n"))
            self.app.invalidate()

        code = await proc.wait()
        self.processing = False
        self.proc = None

        if code == 0:
            if refresh:
                self._refresh_branches()
            if on_success:
                on_success()
            else:
                self.status = "Done. ↑/↓ to move • Enter to switch • Delete to delete"
        else:
            self.status = f"Git exited with code {code}. See output."
            if on_failure:
                on_failure()

        try:
            self.app.layout.focus(self.list_window)
        except Exception:
            pass
        self.app.invalidate()

    def _after_switch(self) -> None:
        """Update UI after successful switch when keeping the app open."""
        self._refresh_branches()
        self.status = "Switched. ↑/↓ to move • Enter to switch • Delete to delete"
        self.app.invalidate()

    async def _send_to_proc(self, text: str) -> None:
        if not self.processing or not self.proc or self.proc.stdin is None:
            return
        payload = (text + "\n").encode()
        self.proc.stdin.write(payload)
        try:
            await self.proc.stdin.drain()
        except Exception:
            pass

    def _start_force_prompt(self, branch: str) -> None:
        """Enter force-delete prompt mode."""
        self.awaiting_force_branch = branch
        self.status = f"Delete failed. FORCE delete {branch}? y/N"
        self.app.invalidate()

    def run(self) -> None:
        if not self.branches:
            print("No branches found.")
            return
        self.app.run()


def ensure_repo(path: str) -> None:
    code, out = run_git_output(["git", "rev-parse", "--is-inside-work-tree"], cwd=path)
    if code != 0 or out.strip() != "true":
        print(f"Not a git repository: {path}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="TUI git branch switcher")
    parser.add_argument(
        "--quit-on-switch",
        action="store_true",
        help="Quit immediately after switching branches",
    )
    args = parser.parse_args()

    repo_path = os.getcwd()
    ensure_repo(repo_path)
    ui = BranchSelectorUI(repo_path, quit_on_switch=args.quit_on_switch)
    ui.run()


if __name__ == "__main__":
    main()
