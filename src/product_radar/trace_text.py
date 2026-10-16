"""Plain-text pages for the research trace (one per command).

Plain labels first, the programme's code in brackets; numbers in one column.
"""

from . import trace_labels as words
from .trace_ai import OUTCOME_WORDS, OUTCOMES
from .trace_funnel import DAY_FIELDS, DAY_WORDS, PART_WORDS, PROBLEM_WORDS

WIDTH = 64


def number(value):
    if isinstance(value, bool) or value is None:
        return "-" if value is None else str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return str(value)


def when(value):
    """A saved time as 'YYYY-MM-DD HH:MM UTC' (or '-')."""
    if not value:
        return "-"
    from .models import timestamp
    from datetime import timezone
    try:
        return timestamp(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:  # a malformed time is shown as saved, never hidden
        return str(value)


def duration(seconds):
    """Seconds, with minutes or hours alongside when long."""
    if seconds is None:
        return "-"
    if seconds >= 3600:
        return f"{number(seconds)} s ({seconds / 3600:.1f} h)"
    if seconds >= 120:
        return f"{number(seconds)} s ({seconds / 60:.0f} min)"
    return f"{number(seconds)} s"


def line(text, value, indent=3):
    text = " " * indent + text
    return f"{text} {'.' * max(2, WIDTH - len(text))} {number(value)}"


def rows(table, indent=5):
    out = [" " * (indent - 2) + table["title"] + ":"]
    for row in table["rows"]:
        out.append(f"{' ' * indent}{number(row['value']):>7}  {row['label']}")
    if table.get("note"):
        out.append(" " * indent + "Note: " + table["note"])
    return out


def header(title, report, extra=None):
    out = [title, "=" * len(title)]
    if "since" in report:
        out.append(f"Period: work saved since {when(report['since'])}" + (f"; standing as of {when(report['as_of'])}"
                                                                           if report.get("as_of") else ""))
    if extra:
        out.extend(extra)
    return out + [""]


def funnel_text(report, *, short=False):
    meter = report["meter"]
    out = header("RESEARCH FUNNEL (every step, in order)", report, [
        "Owner meter now: " + ", ".join(f"{number(meter.get(key))} {text}" for key, text in (
            ("candidates", "product types"), ("accepted_narrow_types", "defined"), ("assessed_types", "checklists"),
            ("assessment_passing_types", "pass the checklist"), ("report_eligible_types", "report-ready"),
            ("report_sets_produced", "report sets"), ("ai_jobs", "AI jobs")))])
    for name, error in sorted(report["errors"].items()):
        out.append(f"!! Could not compute {name}: {words.reason(error)}")
    for item in report["steps"]:
        out.append(item["title"])
        out.extend(line(count["label"], count["value"]) for count in item["counts"])
        for table in item["tables"]:
            if not short or table.get("key"):
                out.extend(rows(table))
        out.extend("   " + note for note in item["notes"])
        out.append("")
    if not short:
        out.append("PER DAY (India time; work first saved that day)")
        days = report["per_day"]
        out.append("   " + "".ljust(34) + "".join(day["day"][5:].rjust(8) for day in days))
        for field in DAY_FIELDS:
            out.append("   " + DAY_WORDS[field].ljust(34) + "".join(number(day[field]).rjust(8) for day in days))
        out.append("")
    return "\n".join(out)


def _timing(item):
    if not item or not item.get("reads_timed"):
        return "not timed"
    return f"average {number(item['average_seconds'])} s, median {number(item['median_seconds'])} s ({item['reads_timed']} reads)"


def _counts(title, counter, labeller, indent=7):
    if not counter:
        return []
    out = [" " * (indent - 2) + title + ":"]
    for code, value in sorted(counter.items(), key=lambda item: (-item[1], str(item[0]))):
        out.append(f"{' ' * indent}{number(value):>6}  {labeller(code)}")
    return out


def _pair(code):
    head, _, tail = str(code).partition(": ")
    return words.state(head) + " — " + words.reason(tail)


def _short_route(row):
    rec, log = row.get("records") or {}, row.get("run_logs") or {}
    parts = []
    if rec:
        parts.append(f"sent {number(rec['sent'])} of {number(rec['planned'])} planned, {number(rec['answers_saved'])} answers, "
                     f"{number(rec['refusals'])} refusals, median {number(rec['median_seconds'])} s, last answer "
                     f"{when(rec['last_answer_at'])}")
    if log:
        parts.append(f"product checks: {number(log['fresh_answers'])} fresh, {number(log['reused_saved_answers'])} reused, "
                     f"{number(sum(log['waits'].values()))} waits, {number(sum(log['failures'].values()))} failures")
    return f"   {row['label']}: " + "; ".join(parts)


def routes_text(report, *, short=False):
    if short:
        out = header("WEBSITE READS (per website, short)", report)
        out.extend(_short_route(row) for row in report["routes"])
        for route, item in report["platforms"].items():
            out.append(f"   {words.route(route)} catalogue: {number(item['reads'])} pages read, last success "
                       f"{when(item['last_success_at'])}" + (f", waiting: {words.reason(item['wait_now'])}" if item["wait_now"] else ""))
        return "\n".join(out)
    out = header("WEBSITE READS (per website)", report)
    for row in report["routes"]:
        out.append(row["label"])
        rec, log = row.get("records"), row.get("run_logs")
        if rec:
            out.append(line("Website reads planned (all steps)", rec["planned"]))
            out.append(line("Website reads sent", rec["sent"]))
            out.append(line("Answers saved", rec["answers_saved"]))
            out.append(line("Planned but not sent yet", rec["not_sent_yet"]))
            out.append(line("Refusals by the site (access blocks)", rec["refusals"]))
            out.append(line("Last answer saved", when(rec["last_answer_at"])))
            out.append("   Time from 'about to send' to 'answer saved': " + _timing(rec))
            if not short:
                out.extend(_counts("Answers by result", rec["answers_by_result"], words.state))
                out.extend(_counts("Unusable answers by reason", rec["unusable_answers_by_reason"], _pair))
                out.extend(_counts("Every planned read by its state now", rec["read_states"], words.state))
                out.extend(_counts("Reads by step", rec["lanes"], str))
        if log:
            out.append("   From the run logs (product-check step only):")
            out.append(line("Reads attempted", log["attempted"], 5))
            out.append(line("Fresh answers", log["fresh_answers"], 5))
            out.append(line("Reused saved answers (no new visit)", log["reused_saved_answers"], 5))
            out.append(line("Earlier failures shown again (no new visit)", log["reused_saved_failures"], 5))
            out.append(line("Failures", sum(log["failures"].values()), 5))
            out.append(line("Waits (question held back)", sum(log["waits"].values()), 5))
            out.append(line("Marked 'blocked' after sending (meter's access blocks)", sum(log["access_blocks"].values()), 5))
            out.append(line("Last fresh answer", when(log["last_success_at"]), 5))
            out.append("     Website answer time: " + _timing(log["website"]))
            out.append("     Whole read step time: " + _timing(log["whole_step"]))
            if not short:
                out.extend(_counts("Failures by reason", log["failures"], words.reason, 9))
                out.extend(_counts("Waits by reason", log["waits"], words.reason, 9))
                out.extend(_counts("Blocked after sending, by reason", log["access_blocks"], words.reason, 9))
        if row.get("note"):
            out.append("   Note: " + row["note"])
        out.append("")
    out.append("DROPSHIPPING PLATFORM CATALOGUE READS")
    for route, item in report["platforms"].items():
        out.append(words.route(route))
        out.append(line("Catalogue pages read", item["reads"]))
        out.append(line("Last successful read", when(item["last_success_at"])))
        out.append(line("Failures in a row now", item["failures_in_a_row"]))
        out.append(line("Waiting now because", words.reason(item["wait_now"]) if item["wait_now"] else "not waiting"))
        out.append("   Time per read: " + _timing(item))
        if not short:
            out.extend(_counts("Reads by result", item["states"], words.state))
            out.extend(_counts("Failed reads by reason", item["failures"], words.reason))
        out.append("")
    out.extend(report["notes"])
    return "\n".join(out)


def _ai_line(bucket):
    outcomes = ", ".join(f"{number(bucket['outcomes'][name])} {OUTCOME_WORDS[name].lower()}"
                         for name in OUTCOMES if bucket["outcomes"].get(name))
    return (f"{number(bucket['jobs'])} jobs ({outcomes}); tokens in {number(bucket['input_tokens'])} "
            f"(cached {number(bucket['cached_input_tokens'])}), out {number(bucket['output_tokens'])}; "
            f"AI time {duration(bucket['total_wall_seconds'])} in total, median {duration(bucket['median_wall_seconds'])} per job"
            + (f"; {number(bucket['retried_jobs'])} retried (earlier attempts: "
               + ", ".join(f"{number(n)} {OUTCOME_WORDS[name].lower()}" for name, n in bucket["earlier_attempts"].items())
               + ")" if bucket.get("retried_jobs") else ""))


def ai_text(report, *, short=False):
    out = header("AI USE (per step)", report)
    out.append("All steps: " + _ai_line(report["total"]))
    if report["other_ai_folders"]:
        out.append("Other AI folders (not counted as jobs): " + ", ".join(
            f"{number(value)} {OUTCOME_WORDS[name].lower()}" for name, value in report["other_ai_folders"].items()))
    out.append("")
    for row in report["steps"]:
        out.append(row["label"])
        out.append("   " + _ai_line(row))
        if row["failures"] and not short:
            out.extend(_counts("Failures and refusals by reason", row["failures"], words.reason))
    out.append("")
    out.append("By day (India time)")
    for row in report["by_day"]:
        out.append(f"   {row['day']}: " + _ai_line(row))
    if not short:
        out.append("")
        out.append("By day and step")
        for row in report["by_day_and_step"]:
            out.append(f"   {row['day']} {words.ai_step(row['step'], None)}: {number(row['jobs'])} jobs, "
                       f"tokens in {number(row['input_tokens'])}, out {number(row['output_tokens'])}, "
                       f"{number(row['total_wall_seconds'])} s")
    out.append("")
    out.extend(_counts("Rounds where AI work waited for the next round", report["round_waits_for_ai"], words.reason, 3))
    if report["ai_steps_that_could_not_start"]:
        out.append("Items whose AI step could not run, by step and reason (one saved stop per item):")
        for row in report["ai_steps_that_could_not_start"]:
            out.append(f"   {number(row['count']):>6}  {row['stage']}: {words.reason(row['reason'])} — "
                       f"{PROBLEM_WORDS.get(row['later'], row['later'])}")
    out.append("")
    out.append("Allowance readings (latest saved reading only)")
    for row in report["allowance_readings_latest"]:
        out.append(f"   {row['text']} (read {when(row['read_at'])})")
    out.append("Allowance alerts over time (every saved alert)")
    for row in report["allowance_alerts"]:
        out.append(f"   {when(row['at'])}  {row['message']}")
    for row in report["allowance_waits_now"]:
        out.append(f"   Waiting now ({row['route']}): {words.reason(row.get('reason'))} since {when(row.get('since'))}"
                   f" until {when(row.get('until'))}")
    out.extend(["", *report["notes"]])
    return "\n".join(out)


def product_text(report):
    out = [f"PRODUCT TYPE JOURNEY: {report['name']} [{report['concept_id']}]", "=" * 40,
           f"Category: {report['category']}", f"What it is: {report['what_it_is']}",
           f"First seen: {when(report['first_seen'])}", ""]
    out.append("Where it came from")
    wave = report["origin"].get("created_by_heading_search")
    if wave:
        out.append(f"   Created by a heading search on {', '.join(wave['routes'])} (wave {wave['wave']}); the wave searched "
                   f"these headings: {', '.join(wave['headings'])}. The wave does not save which listing created the type.")
    for row in report["origin"]["listings"][:30]:
        mark = "created this type" if row["created_type"] else "linked later"
        out.append(f"   {when(row['saved_at'])}  {words.route(row['source']) if row['source'] in words.ROUTES else row['source']}"
                   f" — \"{row['title']}\" (search words: {row['search_words'] or '-'}; heading: {row['heading'] or '-'}; {mark})")
    if len(report["origin"]["listings"]) > 30:
        out.append(f"   ... and {len(report['origin']['listings']) - 30} more listings")
    out.append("")
    out.append("AI decisions about it")
    for row in report["ai_jobs"]:
        out.append(f"   {when(row['finished_at'] or row['created_at'])}  {row['label']}: "
                   f"{OUTCOME_WORDS[row['outcome']].lower()}; tokens in {number(row['input_tokens'])}, "
                   f"out {number(row['output_tokens'])}; {number(row['wall_seconds'])} s")
    out.append("")
    out.append(f"Checklist: {number(report['checklist_meets'])} of {number(report['checklist_total'])} Meets")
    for row in report["checklist"]:
        out.append(f"   {row['id']:>2}. {row['point']}: {row['answer']} — {row['why']}")
    out.append("")
    out.append("Market and supplier questions")
    for row in report["questions"]:
        out.append(f"   {when(row['created_at'])}  {words.route(row['route'])}: {words.state(row['status'])}"
                   f" ({words.reason(row['reason'])}); answer for this type: {words.state(row['answer_for_this_type'])}")
        for change in row["changes"]:
            out.append(f"        {when(change['at'])}  -> {words.state(change['status'])}: {words.reason(change['reason'])}")
    out.append("")
    for row in report["crowding"]:
        out.append(f"Crowding: {row['level']} ({words.reason(row.get('reason'))}; rule {row['rule']}, "
                   f"checked {when(row['assessed_at'])})")
    supplier = report["supplier"]
    out.append(f"Supplier search: {supplier['supplier_search_status']}; leads found {number(supplier['supplier_leads_found'])},"
               f" checked {number(supplier['supplier_leads_checked'])}")
    out.append(f"Photo saved: {'yes' if report['photo'] else 'no'}")
    score = report["score"]
    out.append(f"Score: {number(score['total'])} of 100; label: {words.label(score['label'])}")
    for part, value in score["parts"].items():
        given = ", ".join(f"{name} {item['points']}" for name, item in value["items"].items())
        out.append(f"   {PART_WORDS.get(part, part)}: {value['points']} ({given})")
    out.append("Report-ready: " + ("YES" if report["report_ready"] else "no"))
    for code, text in zip(report["codes"], report["reasons"]):
        out.append(f"   Not ready because: {words.reason(code)} — {text}")
    out.append(f"Allocated to a report: {', '.join(report['allocations']) or 'no'}")
    out.append("")
    out.append(f"Time line ({report['timeline_lines']} saved changes)")
    for row in report["timeline"]:
        summary = "; ".join(f"{key}={value}" for key, value in row["summary"].items()
                            if key not in ("targets", "target_states"))
        out.append(f"   {when(row['at'])}  {row['kind']}: {summary}")
    return "\n".join(out)


def summary_text(summary):
    out = [funnel_text(summary["funnel"], short=True), routes_text(summary["routes"], short=True),
           ai_text(summary["ai"], short=True), "NOT TRACEABLE FROM THE SAVED RECORDS TODAY"]
    out.extend("   - " + item for item in summary["not_traceable"])
    out.append("")
    out.append(f"Read {number(summary['events'])} saved research events and {number(summary['run_log_lines'])} run-log lines "
               f"in {number(summary['seconds'])} s.")
    return "\n\n".join(out[:3]) + "\n\n" + "\n".join(out[3:])
