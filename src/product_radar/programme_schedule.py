"""Legacy direct-WSL task definition; rendering XML never installs or runs it.

The owner computer now uses the native desktop executable for this same task.
scripts/windows/Install-Dropshipper.ps1 owns its exact, action-only upgrade.
Legacy comparison remains strict: it must not replace the graphical launcher
with a visible WSL command or silently change the saved research options.
"""

from datetime import timedelta
from pathlib import PurePosixPath
import re
import subprocess
import xml.etree.ElementTree as ET

from .models import SafetyError, timestamp, utc_now

NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK_NAME = "NeuraFlow-Dropshipper-Research"


def native_task_xml(repo_path, distribution, windows_user, *, live_model=False,
                    funded_quickcommerce_fallback=False, cooperative_turns=False, as_of=None):
    """Resume unfinished work while this owner is logged in and the PC is on.

    Report availability at 09:00 India time is server metadata, not this task.
    No browser/DNS/account setting or Windows service is changed here.
    """
    if (not isinstance(repo_path, str) or not PurePosixPath(repo_path).is_absolute()
            or any(ord(c) < 32 for c in repo_path) or ".." in PurePosixPath(repo_path).parts
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", distribution or "")
            or not re.fullmatch(r"S-1-(?:\d+-)+\d+", windows_user or "")
            or type(live_model) is not bool or type(funded_quickcommerce_fallback) is not bool
            or type(cooperative_turns) is not bool):
        raise SafetyError("PROGRAMME_NATIVE_TASK_INPUT_INVALID")
    ET.register_namespace("", NAMESPACE)
    tag = lambda name: "{" + NAMESPACE + "}" + name
    task = ET.Element(tag("Task"), {"version": "1.3"})
    def child(parent, name, value=None, attributes=None):
        element = ET.SubElement(parent, tag(name), attributes or {})
        if value is not None:
            element.text = value
        return element
    info = child(task, "RegistrationInfo")
    child(info, "Description", "Resume the saved NeuraFlow research programme. Never approve or publish reports.")
    triggers = child(task, "Triggers")
    timer = child(triggers, "TimeTrigger")
    child(timer, "Enabled", "true")
    child(timer, "StartBoundary", (timestamp(as_of or utc_now()) + timedelta(minutes=1)).isoformat(timespec="seconds"))
    repetition = child(timer, "Repetition")
    child(repetition, "Interval", "PT15M")
    child(repetition, "StopAtDurationEnd", "false")
    child(timer, "ExecutionTimeLimit", "PT0S")
    logon = child(triggers, "LogonTrigger")
    child(logon, "Enabled", "true"); child(logon, "ExecutionTimeLimit", "PT0S")
    child(logon, "UserId", windows_user)
    principals = child(task, "Principals")
    principal = child(principals, "Principal", attributes={"id": "Owner"})
    child(principal, "UserId", windows_user)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    child(settings, "MultipleInstancesPolicy", "IgnoreNew")
    child(settings, "DisallowStartIfOnBatteries", "false")
    child(settings, "StopIfGoingOnBatteries", "false")
    child(settings, "StartWhenAvailable", "true")
    child(settings, "AllowStartOnDemand", "true")
    child(settings, "Enabled", "true")
    child(settings, "WakeToRun", "false")
    # The installed owner-computer task exports this supported setting as
    # true. Declare it explicitly; omitted XML defaults to false, so it must
    # not be hidden as a harmless comparison/serialization difference.
    child(settings, "UseUnifiedSchedulingEngine", "true")
    child(settings, "ExecutionTimeLimit", "PT0S")
    child(settings, "Priority", "7")
    actions = child(task, "Actions", attributes={"Context": "Owner"})
    action = child(actions, "Exec")
    child(action, "Command", "wsl.exe")
    args = ["--distribution", distribution, "--cd", repo_path, "--exec",
            str(PurePosixPath(repo_path) / ".venv/bin/python"),
            str(PurePosixPath(repo_path) / "scripts/run_programme.py"), "--tick", "--execute"]
    if live_model:
        args.append("--live-model")
    if funded_quickcommerce_fallback:
        args.append("--funded-quickcommerce-fallback")
    if cooperative_turns:
        args.append("--cooperative-turns")
    child(action, "Arguments", subprocess.list2cmdline(args))
    return ET.tostring(task, encoding="unicode", xml_declaration=False)


def _task_signature(xml):
    """Compare execution meaning, not registration date/pretty-print/prefix."""
    if not isinstance(xml, str) or len(xml.encode("utf-8")) > 256 * 1024 or "<!" in xml:
        raise SafetyError("PROGRAMME_NATIVE_TASK_XML_INVALID")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise SafetyError("PROGRAMME_NATIVE_TASK_XML_INVALID") from None
    tag = lambda name: "{" + NAMESPACE + "}" + name
    if root.tag != tag("Task"):
        raise SafetyError("PROGRAMME_NATIVE_TASK_XML_INVALID")
    def one(parent, name, *, default=None, required=False):
        nodes = parent.findall(tag(name))
        if len(nodes) > 1 or required and len(nodes) != 1:
            raise SafetyError("PROGRAMME_NATIVE_TASK_XML_INVALID")
        if not nodes:
            return default
        return nodes[0]
    def text(parent, name, default=None, *, required=False):
        node = one(parent, name, required=required)
        return default if node is None else (node.text or "").strip()
    def boolean(value):
        if value in ("true", "1"):
            return True
        if value in ("false", "0"):
            return False
        raise SafetyError("PROGRAMME_NATIVE_TASK_XML_INVALID")
    def seconds(value):
        match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", value or "")
        if not match or not any(part is not None for part in match.groups()):
            raise SafetyError("PROGRAMME_NATIVE_TASK_XML_INVALID")
        days, hours, minutes, secs = match.groups()
        return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(secs or 0)
    principal = one(one(root, "Principals", required=True), "Principal", required=True)
    actions = one(root, "Actions", required=True); action = one(actions, "Exec", required=True)
    triggers = one(root, "Triggers", required=True)
    timer = one(triggers, "TimeTrigger", required=True); logon = one(triggers, "LogonTrigger", required=True)
    if len(actions) != 1 or len(triggers) != 2 or len(one(root, "Principals", required=True)) != 1:
        raise SafetyError("PROGRAMME_NATIVE_TASK_XML_INVALID")
    settings = one(root, "Settings", required=True)
    for trigger, allowed in ((timer, ("Enabled", "StartBoundary", "EndBoundary", "Repetition", "ExecutionTimeLimit", "RandomDelay")),
                             (logon, ("Enabled", "StartBoundary", "EndBoundary", "Repetition", "ExecutionTimeLimit", "UserId", "Delay"))):
        positions = [allowed.index(node.tag[len(tag("")):]) if node.tag.startswith(tag(""))
                     and node.tag[len(tag("")):] in allowed else -1 for node in trigger]
        # Windows' registered export and its in-memory definition serialize
        # these same fields in different orders. Meaning is keyed by name;
        # unknown, wrong-namespace and duplicate children still remain invalid.
        if -1 in positions or len(positions) != len(set(positions)):
            raise SafetyError("PROGRAMME_NATIVE_TASK_XML_INVALID")
    result = {"principal_id": principal.get("id"), "action_context": actions.get("Context"),
        "owner": text(principal, "UserId", required=True), "logon_type": text(principal, "LogonType", required=True),
        "run_level": text(principal, "RunLevel", "LeastPrivilege"),
        "group_owner": text(principal, "GroupId"), "required_privileges": ET.tostring(one(principal, "RequiredPrivileges"), encoding="unicode")
            if one(principal, "RequiredPrivileges") is not None else None,
        "command": text(action, "Command", required=True), "arguments": text(action, "Arguments", required=True),
        "working_directory": text(action, "WorkingDirectory"),
        "multiple_instances": text(settings, "MultipleInstancesPolicy", "IgnoreNew"),
        "execution_seconds": seconds(text(settings, "ExecutionTimeLimit", "PT72H")),
        "priority": text(settings, "Priority", "7"),
        "timer_start": timestamp(text(timer, "StartBoundary", required=True)).isoformat(),
        "timer_enabled": boolean(text(timer, "Enabled", "true")),
        "timer_end": text(timer, "EndBoundary"), "timer_random_seconds": seconds(text(timer, "RandomDelay", "PT0S")),
        "timer_execution_seconds": seconds(text(timer, "ExecutionTimeLimit", "PT72H")),
        "logon_enabled": boolean(text(logon, "Enabled", "true")), "logon_owner": text(logon, "UserId", required=True),
        "logon_start": text(logon, "StartBoundary"), "logon_end": text(logon, "EndBoundary"),
        "logon_delay_seconds": seconds(text(logon, "Delay", "PT0S")),
        "logon_execution_seconds": seconds(text(logon, "ExecutionTimeLimit", "PT72H"))}
    repeat = one(timer, "Repetition", required=True)
    result.update(interval_seconds=seconds(text(repeat, "Interval", required=True)),
        duration_seconds=seconds(text(repeat, "Duration")) if text(repeat, "Duration") else None,
        stop_at_duration_end=boolean(text(repeat, "StopAtDurationEnd", "false")),
        logon_repetition=ET.tostring(one(logon, "Repetition"), encoding="unicode") if one(logon, "Repetition") is not None else None,
        restart_on_failure=ET.tostring(one(settings, "RestartOnFailure"), encoding="unicode") if one(settings, "RestartOnFailure") is not None else None,
        network_profile=text(settings, "NetworkProfileName"), delete_after_seconds=seconds(text(settings, "DeleteExpiredTaskAfter", "PT0S")))
    for name, default in (("Enabled", "true"), ("StartWhenAvailable", "false"),
            ("AllowStartOnDemand", "true"), ("DisallowStartIfOnBatteries", "true"),
            ("StopIfGoingOnBatteries", "true"), ("WakeToRun", "false"), ("Hidden", "false"),
            ("RunOnlyIfIdle", "false"), ("RunOnlyIfNetworkAvailable", "false"),
            ("DisallowStartOnRemoteAppSession", "false"), ("AllowHardTerminate", "true"),
            ("UseUnifiedSchedulingEngine", "false")):
        result[name] = boolean(text(settings, name, default))
    return result


def compare_native_task(actual_xml, expected_xml):
    """Read-only comparison. A different existing task is never overwritten."""
    expected = _task_signature(expected_xml)
    try:
        actual = _task_signature(actual_xml)
    except SafetyError:
        return {"matches": False, "differences": ["actual_task_invalid"]}
    differences = [name for name in expected if name != "timer_start" and actual[name] != expected[name]]
    # A correctly installed earlier start remains equivalent. A future changed
    # start can prevent the intended continuation and is not a matching task.
    if timestamp(actual["timer_start"]) > timestamp(expected["timer_start"]):
        differences.append("timer_start")
    return {"matches": not differences, "differences": differences}


def plan_cooperative_task_upgrade(actual_xml, expected_xml):
    """Only add the saved-turn option to the exact existing owner task.

    This pure plan preserves all original XML, including enabled state and
    registration/start times. It cannot install/start a task or expand model,
    billing, identity, distribution, timing or publication authority.
    """
    expected = _task_signature(expected_xml)
    flag = " --cooperative-turns"
    if not expected["arguments"].endswith(flag):
        raise SafetyError("PROGRAMME_NATIVE_TASK_UPGRADE_REFUSED")
    if compare_native_task(actual_xml, expected_xml)["matches"]:
        return {"xml": actual_xml, "changed": False}
    namespace = {"t": NAMESPACE}
    legacy = ET.fromstring(expected_xml)
    legacy.find("t:Actions/t:Exec/t:Arguments", namespace).text = expected["arguments"][:-len(flag)]
    if not compare_native_task(actual_xml, ET.tostring(legacy, encoding="unicode"))["matches"]:
        raise SafetyError("PROGRAMME_NATIVE_TASK_UPGRADE_REFUSED")
    actual = ET.fromstring(actual_xml)
    action = actual.find("t:Actions/t:Exec/t:Arguments", namespace)
    action.text = _task_signature(actual_xml)["arguments"] + flag
    xml = ET.tostring(actual, encoding="unicode")
    if not compare_native_task(xml, expected_xml)["matches"]:
        raise SafetyError("PROGRAMME_NATIVE_TASK_UPGRADE_REFUSED")
    return {"xml": xml, "changed": True}
