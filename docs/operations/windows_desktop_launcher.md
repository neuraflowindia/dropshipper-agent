# Windows desktop and tray controls

Updated: 13 October 2026 (working records moved to the Linux disk). This is the installed owner control for the existing research program. It is not a second research system or proof that the unfinished research/report work is complete.

## Open and use it

Double-click **NeuraFlow Dropshipper** on the Windows desktop. A small graphical window opens. The shortcut, executable, window and tray use the same burgundy package-and-magnifying-glass icon.

| Action | What happens |
|---|---|
| Click **Start**. | The existing saved research switch turns on and the existing research program starts without a command window. Start becomes disabled; Stop becomes enabled. |
| Click **Stop**. | The saved switch turns off first. The controller interrupts only this project's identified research processes. No new scheduled research can start while it is off. |
| Minimize the window. | The window disappears from the taskbar and stays in the notification tray beside the clock. Research keeps its current on/off choice. |
| Click the tray icon once. | The same window opens again. Right-click also provides Open window, Start, Stop, Open logs and Exit. |
| Click the window's **X**. | It hides in the tray; it does not exit or stop research. The window explains this. |
| Click **Exit**. | Research is stopped first, then the graphical application closes and its tray icon is removed. |
| Open the shortcut again while the app is already open or hidden. | The existing window is restored. A second control window or duplicate research run is not started. |

Windows decides whether a tray icon is directly beside the clock or inside the expandable hidden-icons area. Look in that area if the icon is not immediately visible. The app does not change Windows' global notification settings to force placement.

## Understand the status

| Window message | Meaning |
|---|---|
| **Research is running.** | An identified local research process is running. This is not a claim that it has made useful progress or finished a report. |
| **Research is on.** | Research is enabled, but there is no current research process. It can resume at the next scheduled run. |
| **Stopping research…** | The off choice is saved, but an identified process has not exited yet. The app does not falsely report Stopped or allow a conflicting Start. |
| **Research is stopped.** | The off choice is saved and no identified research process is running. Start is enabled and Stop is disabled. |
| **Controls need attention.** | The current state could not be confirmed. The window gives the reason and retries its status check. This is not a successful Stop. |

Start and Stop are both disabled during a change. The window remains responsive while Windows communicates with WSL. Exit waits for an honest stopped state; a kernel-blocked process can take longer to exit. The controller does not forcibly kill all Python processes, shut down WSL, delete locks, reset spending, or touch another project. A provider request already received outside the computer can still finish; its original intent/receipt must be reconciled on the normal research restart, not blindly repeated.

Stop also applies while the research is waiting for an AI answer. This programme explicitly tells its AI runner to preserve an owner interruption instead of treating it as an ordinary failed item and moving on. The runner closes only the helper process group it created, saves the interruption record, and ends the programme. A complete answer that already arrived is saved for reuse on restart; a partial answer is not accepted. A failure to save a diagnostic record must not turn Stop into permission to continue. This behavior is checked with both manufactured failure cases and a real, cost-free local helper process.

The on/off choice survives closing the window and restarting Windows. The existing task still checks every 15 minutes and at sign-in, but now launches the same native executable in its invisible worker mode. While stopped, that check returns without research or a console window. The desktop window itself opens when the owner opens the shortcut; installing it does not add an automatic GUI at sign-in.

## Installed files and ownership

| Item | Location or owner |
|---|---|
| Desktop shortcut | The signed-in owner's Desktop, `NeuraFlow Dropshipper.lnk`. It targets the native executable directly, not a `.cmd`, `.bat`, or PowerShell script. |
| Native executable | `dist/windows/NeuraFlow Dropshipper.exe` under this project. It is a Windows GUI executable, with its icon embedded. |
| Launcher settings | `dist/windows/launcher.json`. It contains only the Windows/Linux project paths, Ubuntu distribution and the research flags copied from the previously installed task. No passwords or provider keys. |
| Icon | `assets/windows/neuraflow-dropshipper.ico`, with seven sizes from 16 to 256 pixels; the companion PNG is its large preview. |
| Existing scheduled task | `NeuraFlow-Dropshipper-Research`. Its owner, triggers, conditions and privileges are preserved. Only its executable action and working directory were changed. |
| Existing owner switch | `data/research/captured/programme_service/control.json` inside the working-records folder, now `/home/anan/neuraflow-dropshipper-data/` on the Linux disk (named in `config/local/runtime_storage.json`). This remains the sole saved research on/off decision. If that folder is missing the controls report an error instead of a default "on". |
| Local action receipts | `data/operations/desktop/YYYY-MM-DD.jsonl` inside the same Linux working-records folder. These record Start, Stop and worker entry/exit; status polling does not create repeated action records. |
| Research output/errors | `data\operations\desktop\windows-YYYY-MM-DD.log` in this project folder on D: (Windows side; **Open logs** opens it). The hidden Windows worker drains and saves the existing program's output. It does not copy account configuration or environment variables. |
| Installation/readback | Written by the Windows installer/test scripts to `data\operations\desktop\` in this project folder on D:. The 11 October `installation.json`, `task-before-<time>.xml` files and test receipts were moved unchanged to `archive/2026-10-13-before-fresh-start/data/operations/desktop/`. |

This launcher's action records do **not** finish the larger planned whole-program logging/progress repair. In particular, Running is not proof of useful progress and the underlying research ordering/timeout/report problems remain separate work.

## Rebuild or reinstall

Exit the desktop app before replacing its executable. Use the already-installed Windows .NET Framework compiler; no new service, installer framework, paid package or administrator account is required.

From PowerShell in this project's folder, run:

```powershell
& '.\scripts\windows\Build-Dropshipper.ps1'
& '.\scripts\windows\Install-Dropshipper.ps1'
& '.\scripts\windows\Install-Dropshipper.ps1' -Install
```

The middle command validates and shows the change without installing it. The final command updates only the existing matching owner task and matching desktop shortcut. It refuses a different task/shortcut, preserves the original research options, checks the task with Windows before updating, saves its previous XML, then reads it back and checks that every non-action setting stayed unchanged. Installation does not start research or change its saved on/off choice.

The older [direct-WSL setup script](../../scripts/setup_programme_task.ps1) is a legacy path and deliberately refuses to overwrite this installed native launcher. Use the desktop installer for this owner computer.

## Verification and limits

| Check group on 11 October | Result | Evidence under `archive/2026-10-13-before-fresh-start/data/operations/desktop` |
|---|---:|---|
| Local project controls, interruption handling, scheduling, saved-answer reuse and existing related research rules. These use manufactured data, not live provider requests. | 296 / 296 passed, across groups of 188 and 108. | `python-controls-tests-20261011.log` and `python-model-reuse-tests-20261011.log`. |
| Native window and tray handlers, with fake research. | 15 / 15 passed. | `gui-self-test.json` and `gui-self-test.png`. |
| Actual installed shortcut, real Start/Stop, hide/restore, Exit, preserved schedule, manually invoked installed task while stopped and icon assignment. | 15 / 15 passed on the final executable. | `installed-test.json` and its dated copy. |

These are focused launcher and affected-code checks, not a full project acceptance run or a new project-completion percentage.

The 11 October installed test exercised the real desktop shortcut, real research Start/Stop, stopping the previous visible research process, both button states, minimize, repeat-shortcut restore, X-to-tray, Exit, manually invoking the actual installed task while stopped, icon assignment and no visible research command window. **All 15 checks passed.** It left research stopped and the owner window open. Receipt: `archive/2026-10-13-before-fresh-start/data/operations/desktop/installed-test.json`. The test's task invocation checks the installed action; it is not evidence that a clock or sign-in trigger fired on its own. Ordinary scheduled restart proof is recorded separately in the connected implementation record.

The existing clock subsequently fired on its own at 11:00:43 UTC on 11 October, after an identified graceful implementation interruption at 10:55:20. The read-only receipt `archive/2026-10-13-before-fresh-start/data/operations/implementation-20261011/natural-clock-after-1100-proof.json` records the unchanged native executable action, one owned research process and its matching admitted operation. No task setting, saved on/off choice or original allowance was changed, and no replacement was manually started. This proves that actual automatic launch, not completion of the later research or the loading of source edits made after launch.

The executable also has a separate `--self-test <receipt-path>` mode. All 15 native checks passed. It exercises the window/tray handlers with a fake research backend, including a failed connection and recovery, an external Windows close request, and the single-click tray restore; it makes no provider call. Native self-test and Python lifecycle tests remain distinct from the actual installed test. The actual button test briefly starts the approved real runner, so do not assume zero provider usage merely because it is called a test.

The installed test records the exact executable's SHA-256 fingerprint and keeps a dated receipt as well as `installed-test.json`. Review found and corrected the external-close case before final acceptance: WinForms reports that direct Windows close request as `TaskManagerClosing`, rather than `UserClosing`. Both window-close requests hide to the tray; an actual Windows shutdown or the app's explicit Exit can still close it. The earlier failed installed test is preserved in `installed-test-before-close-fix-20261011.json`.

For an explicit repeat of the installed test:

```powershell
& '.\scripts\windows\Test-Dropshipper.ps1' -ExerciseResearchControls
```

This actually stops current research, briefly starts it, then stops it again. Do not run it as a passive health check. Normal owner use needs only the desktop shortcut. No full research-history replay, source request or model call is used just to display the launcher's status.
