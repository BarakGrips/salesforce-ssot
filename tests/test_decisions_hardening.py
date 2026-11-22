"""Regression tests for the v0.10 pre-release review of the decision log."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from fixtures import SCRIPTS, make_data_dir

import ssot_common as C
import ssot_decisions as D
from test_decisions import DECISION, Base
from test_search import run

DECISIONS = os.path.join(SCRIPTS, "ssot_decisions.py")


def cli(*args, stdin=None, cwd=None):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "SSOT_DATA_DIR")}
    p = subprocess.run([sys.executable, DECISIONS] + list(args), input=stdin, capture_output=True, env=env,
                       cwd=cwd)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


class StatusInjection(Base):
    def test_backslashes_and_newlines_in_note(self):
        did, _ = self.add()
        with redirect_stdout(io.StringIO()):
            D.cmd_status(self.p, did, "abandoned", note="C:\\new\\d \\1 x\n## D-2026-01-01-01 · injected")
        text = self.log()
        self.assertIn("C:\\new\\d \\1 x ## D-2026-01-01-01 · injected", text)
        self.assertEqual([e[0] for e in D.entries(text)], [did])   # no forged entry

    def test_duplicate_id_refused(self):
        did, _ = self.add()
        dup = self.log() + "\n" + self.log().split("\n\n", 1)[1]   # simulate a merge that duplicated the entry
        D.save(self.p, dup)
        with self.assertRaises(SystemExit):
            D.cmd_status(self.p, did, "built")


class TypeValidation(Base):
    def test_wrong_types_rejected_and_nothing_written(self):
        base = DECISION["needs"][0]
        bad = [
            ["not", "an", "object"],
            {"title": "x", "needs": ["x"]},
            {"title": "x", "needs": [dict(base, rejected={"component": "A"})]},
            {"title": "x", "needs": [dict(base, rejected=["A__c"])]},
            {"title": "x", "needs": [dict(base, terms="expiry")]},
            {"title": "x", "needs": [dict(base, terms=[1])]},
            {"title": "x", "needs": [dict(base, component=["X"])]},
            {"title": ["x"], "needs": [base]},
        ]
        for d in bad:
            with self.assertRaises(SystemExit, msg=repr(d)):
                self.add(d)
        self.assertFalse(os.path.exists(os.path.join(self.d, "decisions.md")))

    def test_carriage_return_cannot_forge_heading(self):
        did, _ = self.add(dict(DECISION, title="Real\r## D-2026-01-01-01 · fake"))
        self.assertEqual([e[0] for e in D.entries(self.log())], [did])


class Concurrency(Base):
    def test_parallel_adds_keep_every_entry(self):
        path = os.path.join(self.tmp.name, "d.json")
        with open(path, "w") as fh:
            json.dump(DECISION, fh)
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "SSOT_DATA_DIR")}
        procs = [subprocess.Popen([sys.executable, DECISIONS, "--data-dir", self.d, "add", path],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env) for _ in range(8)]
        for pr in procs:
            pr.wait()
        ids = [e[0] for e in D.entries(self.log())]
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 8)

    def test_ids_past_99(self):
        text = D.HEADER + "\n## D-2026-01-01-99 · a\n- Status: proposed\n"
        self.assertEqual(D.next_id(text, "2026-01-01"), "D-2026-01-01-100")
        self.assertEqual(D.next_id(text + "\n## D-2026-01-01-100 · b\n", "2026-01-01"), "D-2026-01-01-101")


class SearchScoring(Base):
    def test_status_and_date_lines_do_not_match(self):
        self.add()
        out = run(self.d, "status", "--no-cache", "--no-docs")
        self.assertNotIn("Past decisions", out)

    def test_matching_rejection_is_shown(self):
        self.add()
        out = run(self.d, "quote level", "--no-cache", "--no-docs")
        self.assertIn("Past decisions", out)
        self.assertIn("- Rejected `SBQQ__Quote__c.SBQQ__EndDate__c`", out)

    def test_bad_bytes_in_log_do_not_crash(self):
        self.add()
        with open(os.path.join(self.d, "decisions.md"), "ab") as fh:
            fh.write(b"\n## D-2026-01-02-01 \xb7 caf\xe9 contract end\n- Status: proposed\n")
        out = run(self.d, "contract end", "--no-cache", "--no-docs")
        self.assertIn("Past decisions", out)
        self.assertNotIn("Traceback", out)


class Suggestions(Base):
    def need(self, terms, comp="Contract.EndDate"):
        return {"title": "t", "needs": [{"need": "n", "terms": terms, "verdict": "REUSE", "component": comp}]}

    def test_guardrails(self):
        sugg = D.glossary_suggestions(self.p, self.need(
            ["of", "a b", "Deal_Registration__c", "dealReg", "Opportunity.Amount", "expiry"]))
        self.assertEqual(sugg, ["expiry = contract end date"])

    def test_generic_label_gets_no_suggestion(self):
        # Opportunity.Stage_Default__c is labeled "Stage Default" (fine); a field labeled just "Trial" is not generic,
        # but "Status"/"Type" labels are skipped entirely
        with open(os.path.join(self.p["inventory"], "fields.csv"), "a", encoding="utf-8") as fh:
            fh.write("Lead,Status,Status,picklist,False,,,,,,,,True,False,False,,\n")
        self.assertEqual(D.glossary_suggestions(self.p, self.need(["funnel stage"], "Lead.Status")), [])

    def test_cap_per_need(self):
        sugg = D.glossary_suggestions(self.p, self.need(["expiry", "lapse", "termination", "sunset", "maturity"]))
        self.assertEqual(len(sugg), 3)


class GlossaryBridging(Base):
    def test_refuses_line_that_joins_two_groups(self):
        before = C.read_text(self.p["registry"])
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                D.cmd_glossary_add(self.p, "partners = Pilots")
        self.assertEqual(before, C.read_text(self.p["registry"]))

    def test_merge_announces_the_whole_group(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            D.cmd_glossary_add(self.p, "channel = alliance")
        self.assertIn("partner = reseller = channel = alliance", buf.getvalue())


class Cli(Base):
    def test_stdin_with_bom_and_data_dir_after_subcommand(self):
        raw = ("\ufeff" + json.dumps(DECISION)).encode("utf-8")
        code, out = cli("add", "-", "--data-dir", self.d, stdin=raw)
        self.assertEqual(code, 0, out)
        self.assertIn("logged D-", out)

    def test_data_dir_before_subcommand(self):
        code, out = cli("--data-dir", self.d, "list")
        self.assertEqual(code, 0, out)

    def test_import_from_filesystem_root_does_not_exit(self):
        root = os.path.abspath(os.sep)
        code, out = cli("--data-dir", self.d, "list", cwd=root)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
