"""Regression tests for the final ship-readiness review."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from fixtures import SCRIPTS, make_data_dir

import build_inventory as B
import ssot_common as C
import ssot_decisions as D
import ssot_search as S
from test_search import FakeOrg, run

ENV = {k: v for k, v in os.environ.items() if k not in ("SSOT_ORG", "CLAUDE_PROJECT_DIR", "SSOT_DATA_DIR")}


def main_with(argv, **patches):
    """Run build_inventory.main() with patches; return (exit code, stdout+stderr)."""
    buf = io.StringIO()
    ctx = [mock.patch.object(B, k, v) for k, v in patches.items()]
    ctx += [mock.patch.dict(os.environ, ENV, clear=True), mock.patch("sys.argv", ["build_inventory.py"] + argv)]
    for c in ctx:
        c.start()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            B.main()
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if not isinstance(exc.code, int) and exc.code:
            buf.write(str(exc.code))
    finally:
        for c in reversed(ctx):
            c.stop()
    return code, buf.getvalue()


def snapshot(inv):
    return {f: C.read_text(os.path.join(inv, f)) for f in sorted(os.listdir(inv)) if f.endswith((".csv", ".json"))}


class ExpiredLogin(unittest.TestCase):
    def test_failed_preflight_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_data_dir(tmp, cache_org="prod")
            inv = C.paths(d)["inventory"]
            before = snapshot(inv)
            code, out = main_with(["--org", "prod", "--data-dir", d], require_sf=lambda: None,
                                  _query=mock.Mock(side_effect=RuntimeError("INVALID_SESSION_ID")))
            self.assertEqual(code, 1)
            self.assertIn("Nothing was changed", out)
            self.assertEqual(before, snapshot(inv))
            self.assertTrue(os.path.exists(os.path.join(d, "cache", "metadata", "flows",
                                                        "Contract_Renewal_Subflow.flow-meta.xml")))

    def test_every_query_failing_never_promotes(self):
        """Login check passes, then the session dies: all queries fail -> keep the old inventory."""
        with tempfile.TemporaryDirectory() as tmp:
            d = make_data_dir(tmp)
            inv = C.paths(d)["inventory"]
            before = snapshot(inv)
            fail = mock.Mock(side_effect=RuntimeError("INVALID_SESSION_ID"))
            code, out = main_with(["--org", "prod", "--data-dir", d], require_sf=lambda: None,
                                  preflight=lambda org: None, _query=fail,
                                  describe_object=lambda org, o: (o, RuntimeError("INVALID_SESSION_ID")))
            self.assertEqual(code, 1)
            self.assertIn("previous inventory was kept unchanged", out)
            self.assertEqual(before, snapshot(inv))
            self.assertFalse(os.path.exists(os.path.join(inv, "next")))
            self.assertFalse(os.path.exists(os.path.join(inv, "previous")))


class ExitCodes(unittest.TestCase):
    def test_partial_build_promotes_and_exits_2(self):
        def flows_with_warning(org, id_map, warnings):
            warnings.append("FlowDefinitionView: timeout")
            return []
        with tempfile.TemporaryDirectory() as tmp:
            d = make_data_dir(tmp)
            inv = C.paths(d)["inventory"]
            code, out = main_with(["--org", "prod", "--data-dir", d, "--skip-retrieve"], require_sf=lambda: None,
                                  preflight=lambda org: None, build_objects=lambda org, w: ([], {}, []),
                                  build_fields=lambda *a: [{"object": "Account", "api_name": "X__c"}],
                                  build_flows=flows_with_warning, safe_query=lambda *a, **k: [])
            self.assertEqual(code, 2)
            self.assertIn("X__c", C.read_text(os.path.join(inv, "fields.csv")))
            self.assertEqual(C.read_json(os.path.join(inv, "_meta.json"))["partial_files"], ["flows.csv"])

    def test_retrieve_only_conflicts_with_skip_retrieve(self):
        code, _ = main_with(["--retrieve-only", "--skip-retrieve"])
        self.assertEqual(code, 2)   # argparse usage error


class XmlCache(unittest.TestCase):
    def test_failed_retrieve_keeps_previous_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_data_dir(tmp, cache_org="prod")
            cache = C.paths(d)["cache"]
            warnings = []
            with mock.patch.object(B, "sf_json", side_effect=RuntimeError("INVALID_SESSION_ID")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                n = B.retrieve_cache("prod", cache, ["Account"], warnings)
            self.assertEqual(n, 0)
            self.assertTrue(os.path.exists(os.path.join(cache, "metadata", "flows",
                                                        "Contract_Renewal_Subflow.flow-meta.xml")))
            self.assertFalse(os.path.exists(cache + "-next"))
            self.assertIn("kept the previous XML cache", warnings[-1])

    def test_activity_retrieved_for_task_and_event_fields(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(B, "sf_json", side_effect=lambda args, **k: calls.append(args)), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                B.retrieve_cache("prod", os.path.join(tmp, "cache"), ["Account", "Task", "Event"], [])
        self.assertIn("CustomObject:Activity", calls[0])


class NoInventory(unittest.TestCase):
    def test_search_and_freshness_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "ssot-data")
            os.makedirs(d)
            p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "ssot_search.py"), "--data-dir", d, "renewal"],
                               capture_output=True, text=True, env=ENV)
            self.assertEqual(p.returncode, 1)
            self.assertIn("prove nothing about what exists in the org", p.stdout)
            p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "ssot_search.py"), "--data-dir", d,
                                "--freshness"], capture_output=True, text=True, env=ENV)
            self.assertEqual(p.returncode, 1)


class WhereUsedNotFound(unittest.TestCase):
    def test_missing_field_exits_1(self):
        class Org(FakeOrg):
            def __call__(self, org, soql, tooling=False):
                if "FROM FieldDefinition" in soql:
                    return {"records": []}
                return FakeOrg.__call__(self, org, soql, tooling)
        with tempfile.TemporaryDirectory() as tmp:
            d = make_data_dir(tmp)
            S.P = S.paths(d)
            S.REGISTRY, S.INV, S.CACHE = S.P["registry"], S.P["inventory"], S.P["xml"]
            S.out = S.Out(0)
            orig, S.sf_query = S.sf_query, Org()
            buf = io.StringIO()
            try:
                with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
                    S.where_used("Opportunity.Not_A_Field__c", "prod", 10)
            finally:
                S.sf_query = orig
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Not found", buf.getvalue())


class TemplateAndLog(unittest.TestCase):
    def test_template_ships_no_active_synonyms_and_glossary_add_lands_in_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = C.paths(os.path.join(tmp, "ssot-data"))
            with redirect_stdout(io.StringIO()):
                B.init_data_dir(p)
                self.assertEqual(S.load_glossary(p["registry"]), [])
                D.cmd_glossary_add(p, "expiry = contract end date")
            lines = C.read_text(p["registry"]).splitlines()
            i = lines.index("- expiry = contract end date")
            sec0 = next(n for n, l in enumerate(lines) if l.startswith("## 0"))
            sec1 = next(n for n, l in enumerate(lines) if l.startswith("## 1"))
            self.assertTrue(sec0 + 1 < i < sec1)
            self.assertTrue(lines[i - 1].startswith("Examples"))   # after the explanation, not under the heading
            self.assertEqual(S.load_glossary(p["registry"]), [["expiry", "contract end date"]])

    def test_empty_log_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = C.paths(make_data_dir(tmp))
            buf = io.StringIO()
            with redirect_stdout(buf):
                D.cmd_list(p)
            self.assertIn("no decisions logged yet", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
