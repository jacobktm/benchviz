"""Tests for the Phoronix XML benchmark file parser."""

import tempfile
import unittest
from pathlib import Path

from app import create_app, db
from app.models import Benchmark, BenchmarkResult, System
from app.parser import (
    STRING_PROFILE_FIELDS,
    BOOL_PROFILE_FIELDS,
    _import_notes,
    apply_system_profile,
    parse_file,
    pop_import_notes,
)


def _xml(content: str) -> str:
    return content


SIMPLE_FILE = _xml("""\
<PhoronixTestSuite>
  <System>
    <Identifier>test-sys</Identifier>
    <Hardware>Processor: Test CPU</Hardware>
    <Software>OS: TestOS 1.0</Software>
    <User>tester</User>
    <TimeStamp>2026-01-01 12:00:00</TimeStamp>
  </System>
  <Result>
    <Identifier>pts/bench-1.0.0</Identifier>
    <Title>Test Benchmark</Title>
    <AppVersion>1.0</AppVersion>
    <Description>A simple test</Description>
    <Scale>Seconds</Scale>
    <Proportion>LIB</Proportion>
    <DisplayFormat>BAR_GRAPH</DisplayFormat>
    <Arguments>default</Arguments>
    <Data>
      <Entry>
        <Identifier>test-sys</Identifier>
        <Value>42.5</Value>
      </Entry>
    </Data>
  </Result>
</PhoronixTestSuite>
""")

LINE_GRAPH_FILE = _xml("""\
<PhoronixTestSuite>
  <System>
    <Identifier>line-sys</Identifier>
    <Hardware>Processor: Test CPU</Hardware>
    <Software>OS: TestOS 2.0</Software>
    <User>tester</User>
    <TimeStamp>2026-02-01 12:00:00</TimeStamp>
  </System>
  <Result>
    <Identifier>pts/line-1.0.0</Identifier>
    <Title>Line Benchmark</Title>
    <AppVersion>1.0</AppVersion>
    <Description>A line graph test</Description>
    <Scale>FPS</Scale>
    <Proportion>HIB</Proportion>
    <DisplayFormat>LINE_GRAPH</DisplayFormat>
    <Arguments>high</Arguments>
    <Data>
      <Entry>
        <Identifier>line-sys</Identifier>
        <Value>60.0,55.0,58.0</Value>
      </Entry>
    </Data>
  </Result>
</PhoronixTestSuite>
""")

MULTI_RESULT_FILE = _xml("""\
<PhoronixTestSuite>
  <System>
    <Identifier>multi-sys</Identifier>
    <Hardware>Processor: Test CPU</Hardware>
    <Software>OS: TestOS 3.0</Software>
    <User>tester</User>
    <TimeStamp>2026-03-01 12:00:00</TimeStamp>
  </System>
  <Result>
    <Identifier>pts/multi-a-1.0.0</Identifier>
    <Title>Bench A</Title>
    <AppVersion>1.0</AppVersion>
    <Description>First result</Description>
    <Scale>Seconds</Scale>
    <Proportion>LIB</Proportion>
    <DisplayFormat>BAR_GRAPH</DisplayFormat>
    <Arguments>a</Arguments>
    <Data>
      <Entry>
        <Identifier>multi-sys</Identifier>
        <Value>10.0</Value>
      </Entry>
    </Data>
  </Result>
  <Result>
    <Identifier>pts/multi-b-1.0.0</Identifier>
    <Title>Bench B</Title>
    <AppVersion>1.0</AppVersion>
    <Description>Second result</Description>
    <Scale>Seconds</Scale>
    <Proportion>LIB</Proportion>
    <DisplayFormat>BAR_GRAPH</DisplayFormat>
    <Arguments>b</Arguments>
    <Data>
      <Entry>
        <Identifier>multi-sys</Identifier>
        <Value>20.0</Value>
      </Entry>
    </Data>
  </Result>
</PhoronixTestSuite>
""")


class ParserUnitTest(unittest.TestCase):
    """Tests for helper functions that don't need the DB."""

    def test_pop_import_notes_empty_by_default(self):
        self.assertEqual(pop_import_notes(), [])

    def test_pop_import_notes_returns_and_clears(self):
        _import_notes.append("note 1")
        _import_notes.append("note 2")
        self.assertEqual(pop_import_notes(), ["note 1", "note 2"])
        self.assertEqual(pop_import_notes(), [])

    def test_apply_system_profile_sets_string_fields(self):
        system = System(identifier="test")
        profile = {
            "primary_system_name": "My System",
            "serial_number": "SN001",
            "chassis_version": "v2",
            "cooler_model": "Noctua NH-D15",
            "psu_model": "EVGA 850",
            "psu_wattage": "850W",
            "custom_hardware": "Custom mod",
        }
        apply_system_profile(system, profile)
        self.assertEqual(system.primary_system_name, "My System")
        self.assertEqual(system.serial_number, "SN001")
        self.assertEqual(system.chassis_version, "v2")

    def test_apply_system_profile_skips_empty_values(self):
        system = System(identifier="test")
        profile = {"serial_number": "", "cooler_model": None}
        apply_system_profile(system, profile)
        self.assertIsNone(system.serial_number)

    def test_apply_system_profile_sets_bool_fields(self):
        system = System(identifier="test")
        profile = {"external_off": True, "gpu_fans": True}
        apply_system_profile(system, profile)
        self.assertTrue(system.external_off)
        self.assertTrue(system.gpu_fans)

    def test_apply_system_profile_false_bool(self):
        system = System(identifier="test")
        profile = {"external_off": False}
        apply_system_profile(system, profile)
        self.assertFalse(system.external_off)

    def test_apply_system_profile_sets_primary_name_from_identifier(self):
        """When no primary_system_name is set, it falls back to the identifier."""
        system = System(identifier="my-system")
        apply_system_profile(system, {"external_off": False})
        self.assertEqual(system.primary_system_name, "my-system")

    def test_apply_system_profile_keeps_existing_primary_name(self):
        """An explicit primary_system_name is not overwritten by the identifier."""
        system = System(identifier="sys", primary_system_name="Existing")
        apply_system_profile(system, {"external_off": False})
        self.assertEqual(system.primary_system_name, "Existing")

    def test_apply_system_profile_none_profile(self):
        """None (no profile) is a no-op — primary_system_name stays None."""
        system = System(identifier="test")
        apply_system_profile(system, None)
        self.assertIsNone(system.primary_system_name)


class ParserXmlTest(unittest.TestCase):
    """Tests that parse real XML files against an in-memory database."""

    def setUp(self):
        self.app = create_app()
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _write_and_parse(self, xml_content: str, system_profile=None) -> None:
        with tempfile.NamedTemporaryFile(suffix='.xml', mode='w', delete=False) as f:
            f.write(xml_content)
            tmp_path = f.name
        try:
            parse_file(tmp_path, system_profile=system_profile)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # ── BAR_GRAPH ─────────────────────────────────────────────────

    def test_parse_bar_graph_creates_system(self):
        self._write_and_parse(SIMPLE_FILE)
        systems = System.query.all()
        self.assertEqual(len(systems), 1)
        self.assertEqual(systems[0].identifier, "test-sys")

    def test_parse_bar_graph_creates_benchmark(self):
        self._write_and_parse(SIMPLE_FILE)
        bms = Benchmark.query.all()
        self.assertEqual(len(bms), 1)
        self.assertEqual(bms[0].title, "Test Benchmark")

    def test_parse_bar_graph_creates_result(self):
        self._write_and_parse(SIMPLE_FILE)
        results = BenchmarkResult.query.all()
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].value, 42.5)

    def test_parse_bar_graph_sets_arguments(self):
        self._write_and_parse(SIMPLE_FILE)
        result = BenchmarkResult.query.first()
        self.assertEqual(result.arguments, "default")

    def test_parse_bar_graph_sets_import_batch_id(self):
        self._write_and_parse(SIMPLE_FILE)
        result = BenchmarkResult.query.first()
        self.assertIsNotNone(result.import_batch_id)
        self.assertEqual(len(result.import_batch_id), 36)  # UUID length

    # ── LINE_GRAPH ────────────────────────────────────────────────

    def test_parse_line_graph_stores_data_json(self):
        self._write_and_parse(LINE_GRAPH_FILE)
        result = BenchmarkResult.query.first()
        self.assertEqual(result.data_json, [60.0, 55.0, 58.0])
        self.assertIsNone(result.value)

    def test_parse_line_graph_creates_system(self):
        self._write_and_parse(LINE_GRAPH_FILE)
        self.assertEqual(System.query.count(), 1)
        self.assertEqual(System.query.first().identifier, "line-sys")

    # ── MULTI-RESULT ──────────────────────────────────────────────

    def test_parse_multi_result_creates_two_benchmarks(self):
        self._write_and_parse(MULTI_RESULT_FILE)
        self.assertEqual(Benchmark.query.count(), 2)
        titles = {b.title for b in Benchmark.query.all()}
        self.assertEqual(titles, {"Bench A", "Bench B"})

    def test_parse_multi_result_creates_two_results(self):
        self._write_and_parse(MULTI_RESULT_FILE)
        self.assertEqual(BenchmarkResult.query.count(), 2)

    def test_parse_multi_result_same_system(self):
        self._write_and_parse(MULTI_RESULT_FILE)
        sys_id = System.query.first().id
        for r in BenchmarkResult.query.all():
            self.assertEqual(r.system_id, sys_id)

    # ── EDGE CASES ────────────────────────────────────────────────

    def test_non_phoronix_xml_skipped(self):
        xml = _xml("<NotPhoronix><foo/></NotPhoronix>")
        self._write_and_parse(xml)
        self.assertEqual(System.query.count(), 0)
        self.assertEqual(Benchmark.query.count(), 0)

    def test_missing_system_node_skipped(self):
        xml = _xml("<PhoronixTestSuite><Result/></PhoronixTestSuite>")
        self._write_and_parse(xml)
        self.assertEqual(System.query.count(), 0)

    def test_unknown_entry_system_creates_new_system(self):
        """When an Entry references a system ID not seen before."""
        xml = _xml("""\
<PhoronixTestSuite>
  <System>
    <Identifier>main-sys</Identifier>
    <Hardware>Processor: Main CPU</Hardware>
    <Software>OS: Main</Software>
    <User>tester</User>
    <TimeStamp>2026-01-01</TimeStamp>
  </System>
  <Result>
    <Identifier>pts/bench-1.0.0</Identifier>
    <Title>Bench</Title>
    <AppVersion>1.0</AppVersion>
    <Description>test</Description>
    <Scale>Seconds</Scale>
    <Proportion>LIB</Proportion>
    <DisplayFormat>BAR_GRAPH</DisplayFormat>
    <Arguments>default</Arguments>
    <Data>
      <Entry>
        <Identifier>other-sys</Identifier>
        <Value>10.0</Value>
      </Entry>
    </Data>
  </Result>
</PhoronixTestSuite>
""")
        self._write_and_parse(xml)
        self.assertEqual(System.query.count(), 2)
        idents = {s.identifier for s in System.query.all()}
        self.assertEqual(idents, {"main-sys", "other-sys"})

    def test_empty_data_node_no_crash(self):
        xml = _xml("""\
<PhoronixTestSuite>
  <System>
    <Identifier>empty-sys</Identifier>
    <Hardware>CPU</Hardware>
    <Software>OS</Software>
    <User>tester</User>
    <TimeStamp>2026-01-01</TimeStamp>
  </System>
  <Result>
    <Identifier>pts/empty-1.0.0</Identifier>
    <Title>Empty</Title>
    <AppVersion>1.0</AppVersion>
    <Description>test</Description>
    <Scale>Seconds</Scale>
    <Proportion>LIB</Proportion>
    <DisplayFormat>BAR_GRAPH</DisplayFormat>
    <Arguments>default</Arguments>
  </Result>
</PhoronixTestSuite>
""")
        self._write_and_parse(xml)
        self.assertEqual(Benchmark.query.count(), 1)
        self.assertEqual(BenchmarkResult.query.count(), 0)

    def test_empty_value_no_crash(self):
        xml = _xml("""\
<PhoronixTestSuite>
  <System>
    <Identifier>empty-val-sys</Identifier>
    <Hardware>CPU</Hardware>
    <Software>OS</Software>
    <User>tester</User>
    <TimeStamp>2026-01-01</TimeStamp>
  </System>
  <Result>
    <Identifier>pts/empty-val-1.0.0</Identifier>
    <Title>Empty Val</Title>
    <AppVersion>1.0</AppVersion>
    <Description>test</Description>
    <Scale>Seconds</Scale>
    <Proportion>LIB</Proportion>
    <DisplayFormat>BAR_GRAPH</DisplayFormat>
    <Arguments>default</Arguments>
    <Data>
      <Entry>
        <Identifier>empty-val-sys</Identifier>
        <Value></Value>
      </Entry>
    </Data>
  </Result>
</PhoronixTestSuite>
""")
        self._write_and_parse(xml)
        self.assertEqual(BenchmarkResult.query.count(), 1)

    def test_system_profile_applied(self):
        xml = _xml("""\
<PhoronixTestSuite>
  <System>
    <Identifier>prof-sys</Identifier>
    <Hardware>CPU</Hardware>
    <Software>OS</Software>
    <User>tester</User>
    <TimeStamp>2026-01-01</TimeStamp>
  </System>
  <Result>
    <Identifier>pts/prof-1.0.0</Identifier>
    <Title>Prof</Title>
    <AppVersion>1.0</AppVersion>
    <Description>test</Description>
    <Scale>Seconds</Scale>
    <Proportion>LIB</Proportion>
    <DisplayFormat>BAR_GRAPH</DisplayFormat>
    <Arguments>default</Arguments>
    <Data>
      <Entry>
        <Identifier>prof-sys</Identifier>
        <Value>1.0</Value>
      </Entry>
    </Data>
  </Result>
</PhoronixTestSuite>
""")
        self._write_and_parse(xml, system_profile={
            "serial_number": "SN-PROF-001",
            "cooler_model": "Custom Cooler",
            "psu_wattage": "750W",
            "manual_notes": "Test system",
        })
        sys = System.query.first()
        self.assertEqual(sys.serial_number, "SN-PROF-001")
        self.assertEqual(sys.cooler_model, "Custom Cooler")
        self.assertEqual(sys.psu_wattage, "750W")
        self.assertEqual(sys.manual_notes, "Test system")

    def test_perf_counter_not_primary(self):
        """Perf counter benchmarks should not be marked primary."""
        xml = _xml("""\
<PhoronixTestSuite>
  <System>
    <Identifier>perf-sys</Identifier>
    <Hardware>CPU</Hardware>
    <Software>OS</Software>
    <User>tester</User>
    <TimeStamp>2026-01-01</TimeStamp>
  </System>
  <Result>
    <Identifier>perf bench-1.0.0</Identifier>
    <Title>Bench</Title>
    <AppVersion>1.0</AppVersion>
    <Description>perf stat test</Description>
    <Scale>counts</Scale>
    <Proportion>HIB</Proportion>
    <DisplayFormat>BAR_GRAPH</DisplayFormat>
    <Arguments>perf stat -e cycles</Arguments>
    <Data>
      <Entry>
        <Identifier>perf-sys</Identifier>
        <Value>1000</Value>
      </Entry>
    </Data>
  </Result>
</PhoronixTestSuite>
""")
        self._write_and_parse(xml)
        bm = Benchmark.query.first()
        self.assertFalse(bm.is_primary)

    def test_non_perf_bar_graph_is_primary(self):
        """Regular BAR_GRAPH benchmarks are marked primary."""
        self._write_and_parse(SIMPLE_FILE)
        bm = Benchmark.query.first()
        self.assertTrue(bm.is_primary)


if __name__ == "__main__":
    unittest.main()
